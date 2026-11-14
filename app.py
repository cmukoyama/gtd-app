"""GTD app: Flask + SQLite, single user, password protected."""
import hmac
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)

STATUSES = ("inbox", "next", "waiting", "someday", "done", "trash")
DEFAULT_CONTEXTS = ["computador", "telefone", "rua", "casa", "reunião"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id {pk},
    title TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',      -- active | someday | done
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id {pk},
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'inbox',
    context TEXT NOT NULL DEFAULT '',
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    due TEXT,
    waiting_on TEXT NOT NULL DEFAULT '',
    minutes INTEGER,
    focus_on TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_project ON items(project_id);
CREATE TABLE IF NOT EXISTS reviews (
    id {pk},
    done_at TEXT NOT NULL
);
"""


TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Sao_Paulo"))
MINUTES = (5, 15, 30, 60, 120)


def now():
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def local_today():
    return datetime.now(TZ).date()


class Db:
    """Camada fina: Postgres (DATABASE_URL, ex. Neon) ou SQLite local (DB_PATH)."""

    def __init__(self, url, path):
        self.pg = bool(url)
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None: compatível com o pooler (pgbouncer) do Neon
            self.conn = psycopg.connect(url, row_factory=dict_row,
                                        prepare_threshold=None)
        else:
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, args=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, tuple(args))

    def insert(self, sql, args=()):
        """INSERT ... e devolve o id gerado."""
        return self.execute(sql + " RETURNING id", args).fetchone()["id"]

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def init_schema(self):
        pk = "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        ddl = SCHEMA.format(pk=pk)
        if self.pg:
            self.conn.execute(ddl)
        else:
            self.conn.executescript(ddl)
        # migração de bancos criados antes das colunas minutes/focus_on
        if self.pg:
            self.conn.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS minutes INTEGER")
            self.conn.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS focus_on TEXT")
        else:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
            for name in ("minutes", "focus_on"):
                if name not in cols:
                    self.conn.execute(
                        f"ALTER TABLE items ADD COLUMN {name} "
                        + ("INTEGER" if name == "minutes" else "TEXT"))
        self.conn.commit()


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DB_PATH=os.environ.get("DB_PATH", "gtd.db"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        APP_PASSWORD=os.environ.get("APP_PASSWORD", ""),
        SECRET_KEY=os.environ.get("SECRET_KEY", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("INSECURE_COOKIES") != "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if config:
        app.config.update(config)
    if not app.config["APP_PASSWORD"] or not app.config["SECRET_KEY"]:
        raise RuntimeError("Defina APP_PASSWORD e SECRET_KEY no ambiente.")

    # ---------- db ----------
    schema_ready = {"ok": False}

    def db():
        if "db" not in g:
            g.db = Db(app.config["DATABASE_URL"], app.config["DB_PATH"])
            if not schema_ready["ok"]:
                g.db.init_schema()
                schema_ready["ok"] = True
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ---------- auth + csrf ----------
    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not session.get("auth"):
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "CSRF inválido")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ok = hmac.compare_digest(request.form.get("password", ""),
                                     app.config["APP_PASSWORD"])
            if ok:
                session.clear()
                session["auth"] = True
                session.permanent = True
                nxt = request.args.get("next", "")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("inbox")
                return redirect(nxt)
            flash("Senha incorreta.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- helpers ----------
    def counts():
        rows = db().execute(
            "SELECT status, COUNT(*) c FROM items GROUP BY status").fetchall()
        c = {r["status"]: r["c"] for r in rows}
        return c

    @app.context_processor
    def inject():
        if not session.get("auth"):
            return {}
        t = local_today().isoformat()
        n = db().execute(
            "SELECT COUNT(*) c FROM items WHERE status='next' AND focus_on=?",
            (t,)).fetchone()["c"]
        return {"counts": counts(), "today": t, "today_count": n}

    def contexts():
        used = [r["context"] for r in db().execute(
            "SELECT DISTINCT context FROM items WHERE context != ''")]
        return sorted(set(DEFAULT_CONTEXTS) | set(used))

    def active_projects():
        return db().execute(
            "SELECT * FROM projects WHERE status='active' ORDER BY title"
        ).fetchall()

    def clean(value, limit=500):
        return (value or "").strip()[:limit]

    def clean_context(value):
        return clean(value, 40).lstrip("@").lower()

    def clean_date(value):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None

    def clean_minutes(value):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        return v if v in MINUTES else None

    def project_id_or_none(value):
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        row = db().execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
        return row["id"] if row else None

    def back():
        nxt = request.form.get("next", "")
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("inbox"))

    # ---------- inbox / capture ----------
    @app.get("/")
    @app.get("/inbox")
    @login_required
    def inbox():
        items = db().execute(
            "SELECT * FROM items WHERE status='inbox' ORDER BY id").fetchall()
        return render_template("inbox.html", items=items,
                               contexts=contexts(), projects=active_projects())

    @app.post("/capture")
    @login_required
    def capture():
        # aceita várias linhas: cada linha vira um item no Inbox
        lines = [clean(x, 300) for x in request.form.get("title", "").splitlines()]
        lines = [x for x in lines if x]
        for title in lines[:50]:
            db().execute(
                "INSERT INTO items (title, status, created_at) VALUES (?, 'inbox', ?)",
                (title, now()))
        db().commit()
        return back()

    # ---------- clarify ----------
    @app.post("/items/<int:item_id>/clarify")
    @login_required
    def clarify(item_id):
        row = db().execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            abort(404)
        decision = request.form.get("decision", "")
        title = clean(request.form.get("title"), 300)
        if not title:
            flash("O título não pode ficar vazio.")
            return back()
        notes = clean(request.form.get("notes"), 4000)
        ctx = clean_context(request.form.get("context"))
        pid = project_id_or_none(request.form.get("project_id"))
        due = clean_date(request.form.get("due"))
        waiting_on = clean(request.form.get("waiting_on"), 100)
        minutes = clean_minutes(request.form.get("minutes"))

        if decision == "next":
            status = "next"
        elif decision == "waiting":
            status = "waiting"
        elif decision == "someday":
            status, ctx = "someday", ""
        elif decision == "trash":
            status = "trash"
        elif decision == "done":  # regra dos 2 minutos
            status = "done"
        else:
            abort(400, "Decisão inválida")
        done_at = now() if status == "done" else None
        db().execute(
            """UPDATE items SET title=?, notes=?, status=?, context=?, project_id=?,
               due=?, waiting_on=?, minutes=?, completed_at=? WHERE id=?""",
            (title, notes, status, ctx, pid, due, waiting_on, minutes, done_at,
             item_id))
        db().commit()
        return back()

    # ---------- edit ----------
    @app.get("/items/<int:item_id>/edit")
    @login_required
    def item_edit(item_id):
        item = db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            abort(404)
        nxt = request.args.get("next", "")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = url_for("next_actions")
        return render_template("edit.html", i=item, next=nxt,
                               contexts=contexts(), projects=active_projects())

    @app.post("/items/<int:item_id>/edit")
    @login_required
    def item_edit_save(item_id):
        if not db().execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            abort(404)
        title = clean(request.form.get("title"), 300)
        if not title:
            flash("O título não pode ficar vazio.")
            return redirect(url_for("item_edit", item_id=item_id,
                                    next=request.form.get("next", "")))
        status = request.form.get("status", "next")
        if status not in ("inbox", "next", "waiting", "someday"):
            abort(400)
        ctx = "" if status == "someday" else clean_context(request.form.get("context"))
        db().execute(
            """UPDATE items SET title=?, notes=?, status=?, context=?, project_id=?,
               due=?, waiting_on=?, minutes=? WHERE id=?""",
            (title, clean(request.form.get("notes"), 4000), status, ctx,
             project_id_or_none(request.form.get("project_id")),
             clean_date(request.form.get("due")),
             clean(request.form.get("waiting_on"), 100),
             clean_minutes(request.form.get("minutes")), item_id))
        db().commit()
        return back()

    # ---------- today ----------
    @app.get("/today")
    @login_required
    def today_view():
        t = local_today().isoformat()
        base = """SELECT i.*, p.title AS project_title FROM items i
                  LEFT JOIN projects p ON p.id = i.project_id
                  WHERE i.status='next' AND """
        items = db().execute(
            base + "i.focus_on = ? ORDER BY (i.due IS NULL), i.due, i.id", (t,)
        ).fetchall()
        carry = db().execute(
            base + "i.focus_on IS NOT NULL AND i.focus_on < ? ORDER BY i.focus_on, i.id",
            (t,)).fetchall()
        total = sum((i["minutes"] or 0) for i in items)
        return render_template("today.html", items=items, carry=carry, total=total)

    @app.post("/items/<int:item_id>/today")
    @login_required
    def item_today(item_id):
        on = request.form.get("on") == "1"
        cur = db().execute(
            "UPDATE items SET focus_on=? WHERE id=? AND status='next'",
            (local_today().isoformat() if on else None, item_id))
        if cur.rowcount == 0:
            abort(404)
        db().commit()
        return back()

    # ---------- lists ----------
    @app.get("/next")
    @login_required
    def next_actions():
        ctx = clean_context(request.args.get("context"))
        sql = ("""SELECT i.*, p.title AS project_title FROM items i
                  LEFT JOIN projects p ON p.id = i.project_id
                  WHERE i.status='next'""")
        args = []
        if ctx == "-":
            sql += " AND i.context = ''"
        elif ctx:
            sql += " AND i.context = ?"
            args.append(ctx)
        tempo = clean_minutes(request.args.get("tempo"))
        if tempo:
            sql += " AND i.minutes IS NOT NULL AND i.minutes <= ?"
            args.append(tempo)
        sql += " ORDER BY (i.due IS NULL), i.due, i.id"
        items = db().execute(sql, args).fetchall()
        return render_template("next.html", items=items, ctx=ctx, tempo=tempo,
                               minutes_options=MINUTES,
                               contexts=contexts(), projects=active_projects())

    @app.get("/waiting")
    @login_required
    def waiting():
        items = db().execute(
            """SELECT i.*, p.title AS project_title FROM items i
               LEFT JOIN projects p ON p.id = i.project_id
               WHERE i.status='waiting' ORDER BY i.created_at""").fetchall()
        return render_template("waiting.html", items=items)

    @app.get("/someday")
    @login_required
    def someday():
        items = db().execute(
            "SELECT * FROM items WHERE status='someday' ORDER BY id").fetchall()
        projects = db().execute(
            "SELECT * FROM projects WHERE status='someday' ORDER BY title").fetchall()
        return render_template("someday.html", items=items, projects=projects)

    @app.get("/done")
    @login_required
    def done():
        items = db().execute(
            """SELECT i.*, p.title AS project_title FROM items i
               LEFT JOIN projects p ON p.id = i.project_id
               WHERE i.status='done' ORDER BY i.completed_at DESC LIMIT 100"""
        ).fetchall()
        return render_template("done.html", items=items)

    # ---------- item actions ----------
    @app.post("/items/<int:item_id>/set")
    @login_required
    def item_set(item_id):
        """Mudança rápida de estado: done, next, waiting, someday, inbox, trash."""
        status = request.form.get("status", "")
        if status not in STATUSES:
            abort(400)
        done_at = now() if status == "done" else None
        cur = db().execute(
            "UPDATE items SET status=?, completed_at=? WHERE id=?",
            (status, done_at, item_id))
        if cur.rowcount == 0:
            abort(404)
        db().commit()
        return back()

    @app.post("/items/<int:item_id>/delete")
    @login_required
    def item_delete(item_id):
        db().execute("DELETE FROM items WHERE id=?", (item_id,))
        db().commit()
        return back()

    # ---------- projects ----------
    @app.get("/projects")
    @login_required
    def projects():
        rows = db().execute(
            """SELECT p.*,
                 (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id AND i.status='next') AS n_next,
                 (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id AND i.status='waiting') AS n_wait
               FROM projects p WHERE p.status='active' ORDER BY p.title""").fetchall()
        return render_template("projects.html", projects=rows)

    @app.post("/projects")
    @login_required
    def project_create():
        title = clean(request.form.get("title"), 200)
        if not title:
            flash("Dê um nome ao projeto.")
            return redirect(url_for("projects"))
        new_id = db().insert(
            "INSERT INTO projects (title, outcome, created_at) VALUES (?, ?, ?)",
            (title, clean(request.form.get("outcome"), 1000), now()))
        db().commit()
        return redirect(url_for("project_view", project_id=new_id))

    @app.get("/projects/<int:project_id>")
    @login_required
    def project_view(project_id):
        p = db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p:
            abort(404)
        items = db().execute(
            """SELECT * FROM items WHERE project_id=? AND status IN ('next','waiting','someday')
               ORDER BY CASE status WHEN 'next' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END, id""",
            (project_id,)).fetchall()
        done_items = db().execute(
            "SELECT * FROM items WHERE project_id=? AND status='done' ORDER BY completed_at DESC LIMIT 20",
            (project_id,)).fetchall()
        return render_template("project.html", p=p, items=items,
                               done_items=done_items, contexts=contexts())

    @app.post("/projects/<int:project_id>/actions")
    @login_required
    def project_add_action(project_id):
        if not db().execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            abort(404)
        title = clean(request.form.get("title"), 300)
        if title:
            db().execute(
                """INSERT INTO items (title, status, context, project_id, created_at)
                   VALUES (?, 'next', ?, ?, ?)""",
                (title, clean_context(request.form.get("context")), project_id, now()))
            db().commit()
        return redirect(url_for("project_view", project_id=project_id))

    @app.post("/projects/<int:project_id>/set")
    @login_required
    def project_set(project_id):
        status = request.form.get("status", "")
        if status not in ("active", "someday", "done"):
            abort(400)
        done_at = now() if status == "done" else None
        cur = db().execute(
            "UPDATE projects SET status=?, completed_at=? WHERE id=?",
            (status, done_at, project_id))
        if cur.rowcount == 0:
            abort(404)
        db().commit()
        return redirect(url_for("projects"))

    # ---------- weekly review ----------
    def review_data():
        d = db()
        stalled = d.execute(
            """SELECT p.* FROM projects p WHERE p.status='active' AND NOT EXISTS
               (SELECT 1 FROM items i WHERE i.project_id=p.id AND i.status='next')
               ORDER BY p.title""").fetchall()
        cutoff = (local_today() - timedelta(days=7)).isoformat()
        old_waiting = d.execute(
            "SELECT * FROM items WHERE status='waiting' AND created_at < ? ORDER BY created_at",
            (cutoff,)).fetchall()
        overdue = d.execute(
            "SELECT * FROM items WHERE status='next' AND due IS NOT NULL AND due < ? ORDER BY due",
            (local_today().isoformat(),)).fetchall()
        last = d.execute("SELECT done_at FROM reviews ORDER BY id DESC LIMIT 1").fetchone()
        return stalled, old_waiting, overdue, (last["done_at"] if last else None)

    @app.get("/review")
    @login_required
    def review():
        stalled, old_waiting, overdue, last = review_data()
        return render_template("review.html", stalled=stalled,
                               old_waiting=old_waiting, overdue=overdue, last=last)

    @app.post("/review/finish")
    @login_required
    def review_finish():
        db().execute("INSERT INTO reviews (done_at) VALUES (?)", (now(),))
        db().commit()
        flash("Revisão semanal registrada.")
        return redirect(url_for("review"))

    return app


app = None
if os.environ.get("APP_PASSWORD") and os.environ.get("SECRET_KEY"):
    app = create_app()  # usado pelo gunicorn: gunicorn app:app

if __name__ == "__main__":
    create_app().run(debug=False, port=int(os.environ.get("PORT", 8000)))
