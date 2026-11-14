import os
import re

import pytest

from app import create_app

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def client(request, tmp_path):
    cfg = {"DB_PATH": str(tmp_path / "t.db"), "DATABASE_URL": "",
           "APP_PASSWORD": "pw", "SECRET_KEY": "k",
           "SESSION_COOKIE_SECURE": False, "TESTING": True}
    if request.param == "postgres":
        import psycopg
        # APAGA as tabelas: use só um banco de teste, nunca o de produção
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS items, projects, reviews CASCADE")
        cfg["DATABASE_URL"] = PG_URL
    return create_app(cfg).test_client()


def token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client):
    t = token(client)
    client.post("/login", data={"password": "pw", "csrf": t})
    return token(client, "/inbox")


def post(client, url, t, **data):
    return client.post(url, data={"csrf": t, **data}, follow_redirects=True)


def test_requires_login(client):
    assert client.get("/inbox").status_code == 302


def test_wrong_password(client):
    t = token(client)
    r = client.post("/login", data={"password": "x", "csrf": t})
    assert b"Senha incorreta" in r.data


def test_csrf_required(client):
    login(client)
    assert client.post("/capture", data={"title": "x"}).status_code == 400


def test_flow_capture_clarify_done(client):
    t = login(client)
    post(client, "/capture", t, title="Ligar para banco\nComprar café", next="/inbox")
    html = client.get("/inbox").get_data(as_text=True)
    assert "Ligar para banco" in html and "Comprar café" in html
    post(client, "/items/1/clarify", t, decision="next", title="Ligar para banco",
         context="@Telefone")
    html = client.get("/next?context=telefone").get_data(as_text=True)
    assert "Ligar para banco" in html and "@telefone" in html
    post(client, "/items/1/set", t, status="done")
    assert "Ligar para banco" not in client.get("/next").get_data(as_text=True)
    assert "Ligar para banco" in client.get("/done").get_data(as_text=True)


def test_project_without_next_action_flagged_in_review(client):
    t = login(client)
    post(client, "/projects", t, title="Reformar escritório")
    assert "Reformar escritório" in client.get("/review").get_data(as_text=True)
    post(client, "/projects/1/actions", t, title="Pedir orçamento", context="telefone")
    html = client.get("/review").get_data(as_text=True)
    assert "Todos os projetos ativos têm próxima ação" in html


def test_waiting_and_someday(client):
    t = login(client)
    post(client, "/capture", t, title="Resposta do fornecedor\nAprender violão")
    post(client, "/items/1/clarify", t, decision="waiting",
         title="Resposta do fornecedor", waiting_on="Zé")
    post(client, "/items/2/clarify", t, decision="someday", title="Aprender violão")
    assert "Zé" in client.get("/waiting").get_data(as_text=True)
    assert "Aprender violão" in client.get("/someday").get_data(as_text=True)


def test_invalid_decision_and_open_redirect(client):
    t = login(client)
    post(client, "/capture", t, title="x")
    assert client.post("/items/1/clarify", data={"csrf": t, "decision": "foo",
                       "title": "x"}).status_code == 400
    r = client.post("/capture", data={"csrf": t, "title": "y",
                    "next": "//evil.com"})
    assert r.headers["Location"].endswith("/inbox")


def test_html_escaped(client):
    t = login(client)
    post(client, "/capture", t, title="<script>alert(1)</script>")
    assert "<script>alert(1)" not in client.get("/inbox").get_data(as_text=True)


def test_refuses_without_secrets(tmp_path):
    with pytest.raises(RuntimeError):
        create_app({"DB_PATH": str(tmp_path / "a.db"), "DATABASE_URL": "",
                    "APP_PASSWORD": "", "SECRET_KEY": ""})


def test_edit_item(client):
    t = login(client)
    post(client, "/capture", t, title="refinar HU")
    post(client, "/items/1/clarify", t, decision="next", title="refinar HU",
         context="computador")
    assert "editar" in client.get("/next").get_data(as_text=True)
    page = client.get("/items/1/edit?next=/next").get_data(as_text=True)
    assert 'value="refinar HU"' in page
    post(client, "/items/1/edit", t, title="refinar HU para planning", status="next",
         context="@Foco", due="2026-12-01", notes="rever critérios", next="/next")
    html = client.get("/next").get_data(as_text=True)
    assert "refinar HU para planning" in html and "@foco" in html and "2026-12-01" in html
    post(client, "/items/1/edit", t, title="refinar HU para planning",
         status="waiting", waiting_on="Zé", next="/next")
    assert "Zé" in client.get("/waiting").get_data(as_text=True)
    assert client.post("/items/1/edit", data={"csrf": t, "title": "x",
                       "status": "done"}).status_code == 400
    assert client.get("/items/99/edit").status_code == 404


def _next_item(client, t, title="refinar HU", **extra):
    post(client, "/capture", t, title=title)
    iid = int(re.findall(r"/items/(\d+)/clarify", client.get("/inbox").get_data(as_text=True))[-1])
    post(client, f"/items/{iid}/clarify", t, decision="next", title=title, **extra)
    return iid


def test_today_and_minutes(client):
    t = login(client)
    a = _next_item(client, t, "tarefa curta", minutes="15", context="foco")
    b = _next_item(client, t, "tarefa longa", minutes="120")
    assert "tarefa" not in client.get("/today").get_data(as_text=True).split("<h1>Hoje</h1>")[1].split("</main>")[0].replace("Nada escolhido", "")
    post(client, f"/items/{a}/today", t, on="1", next="/next")
    html = client.get("/today").get_data(as_text=True)
    assert "tarefa curta" in html and "tarefa longa" not in html and "15 min estimados" in html
    # filtro por tempo
    short = client.get("/next?tempo=30").get_data(as_text=True)
    assert "tarefa curta" in short and "tarefa longa" not in short
    post(client, f"/items/{a}/today", t, on="0", next="/next")
    assert "tarefa curta" not in client.get("/today").get_data(as_text=True).split("<h1>Hoje</h1>")[1].split("</main>")[0]
    # tempo inválido é ignorado
    assert client.get("/next?tempo=abc").status_code == 200
    # edição altera tempo
    post(client, f"/items/{b}/edit", t, title="tarefa longa", status="next", minutes="30", next="/next")
    assert "tarefa longa" in client.get("/next?tempo=30").get_data(as_text=True)


def test_today_only_for_next_items(client):
    t = login(client)
    post(client, "/capture", t, title="solto")
    assert client.post("/items/1/today", data={"csrf": t, "on": "1"}).status_code == 404


def test_migrates_old_sqlite_schema(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript("""CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, completed_at TEXT);
            CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'inbox',
            context TEXT NOT NULL DEFAULT '', project_id INTEGER, due TEXT,
            waiting_on TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, completed_at TEXT);
            INSERT INTO items (title, status, created_at) VALUES ('antiga', 'next', '2026-01-01T00:00:00');""")
    c = create_app({"DB_PATH": str(path), "DATABASE_URL": "", "APP_PASSWORD": "pw",
                    "SECRET_KEY": "k", "SESSION_COOKIE_SECURE": False}).test_client()
    login(c)
    assert "antiga" in c.get("/next").get_data(as_text=True)
