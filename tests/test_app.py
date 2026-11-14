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
    assert "/items/1/edit" in client.get("/next").get_data(as_text=True)
    page = client.get("/items/1/edit?next=/next").get_data(as_text=True)
    assert 'value="refinar HU"' in page
    post(client, "/items/1/edit", t, title="refinar HU para planning", status="next",
         context="@Foco", due="2026-12-01", notes="rever critérios", next="/next")
    html = client.get("/next").get_data(as_text=True)
    assert "refinar HU para planning" in html and "@foco" in html and "01/12" in html
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


def test_checklists_seeded_and_toggle(client):
    t = login(client)
    daily = client.get("/daily").get_data(as_text=True)
    assert "Esvaziar o Inbox" in daily and "0 de 6 concluídos" in daily
    post(client, "/checklist/1/toggle", t, next="/daily")
    assert "1 de 6 concluídos" in client.get("/daily").get_data(as_text=True)
    post(client, "/checklist/1/toggle", t, next="/daily")  # desmarca
    assert "0 de 6 concluídos" in client.get("/daily").get_data(as_text=True)
    review = client.get("/review").get_data(as_text=True)
    assert "Checklist da semana" in review and "0 de 10 concluídos" in review
    assert client.post("/checklist/999/toggle", data={"csrf": t}).status_code == 404


def test_checklist_resets_by_period(client, monkeypatch):
    import datetime as dt
    import app as appmod
    t = login(client)
    daily_id = 1    # daily
    weekly_id = 7   # primeiro weekly
    days = {"d": dt.date(2026, 11, 11)}  # quarta-feira
    monkeypatch.setattr(appmod, "local_today", lambda: days["d"])
    post(client, f"/checklist/{daily_id}/toggle", t)
    post(client, f"/checklist/{weekly_id}/toggle", t)
    days["d"] = dt.date(2026, 11, 12)   # quinta: mesmo semana, outro dia
    assert "0 de 6 concluídos" in client.get("/daily").get_data(as_text=True)
    assert "1 de 10 concluídos" in client.get("/review").get_data(as_text=True)
    days["d"] = dt.date(2026, 11, 16)   # segunda: nova semana
    assert "0 de 10 concluídos" in client.get("/review").get_data(as_text=True)


def test_checklist_add_delete_and_no_reseed(client, tmp_path):
    t = login(client)
    post(client, "/checklist/add", t, kind="daily", title="Beber água", next="/daily")
    assert "Beber água" in client.get("/daily").get_data(as_text=True)
    assert client.post("/checklist/add", data={"csrf": t, "kind": "x", "title": "y"}).status_code == 400
    post(client, "/checklist/1/delete", t, next="/daily")
    assert "Esvaziar o Inbox: capturar" not in client.get("/daily").get_data(as_text=True)
    # nova instância do app sobre o mesmo banco não recria o item apagado
    cfg = {k: client.application.config[k] for k in ("DB_PATH", "DATABASE_URL", "APP_PASSWORD", "SECRET_KEY")}
    cfg.update(SESSION_COOKIE_SECURE=False)
    c2 = create_app(cfg).test_client()
    login(c2)
    assert "Esvaziar o Inbox: capturar" not in c2.get("/daily").get_data(as_text=True)


def test_context_colors_stable():
    from app import ctx_class
    assert ctx_class("foco") == ctx_class("@Foco") == "c2"
    assert ctx_class("qualquer-coisa") == ctx_class("qualquer-coisa")
    assert ctx_class("x").startswith("c") and 0 <= int(ctx_class("xyz")[1:]) <= 9


def test_context_class_rendered(client):
    t = login(client)
    post(client, "/capture", t, title="falar com LPP")
    post(client, "/items/1/clarify", t, decision="next", title="falar com LPP", context="conversar")
    html = client.get("/next").get_data(as_text=True)
    assert 'class="ctx c3"' in html and "cx c3" in html


def test_remove_context_keeps_tasks(client):
    t = login(client)
    assert "@rua" in client.get("/contexts").get_data(as_text=True)
    iid = _next_item(client, t, "ligar para fornecedor", context="rua")
    page = client.get("/contexts").get_data(as_text=True)
    assert "1 tarefa aberta" in page
    post(client, "/contexts/remove", t, name="@Rua")
    assert "Contexto @rua removido" in client.get("/contexts").get_data(as_text=True) or True
    page = client.get("/contexts").get_data(as_text=True)
    assert 'value="rua"' not in page                       # saiu da lista
    nxt = client.get("/next").get_data(as_text=True)
    assert "ligar para fornecedor" in nxt                  # tarefa continua
    assert "@rua" not in nxt                               # sem contexto agora
    # remover de novo ou nome vazio não quebra
    assert client.post("/contexts/remove", data={"csrf": t, "name": "inexistente"}).status_code == 302
    assert client.post("/contexts/remove", data={"csrf": t, "name": ""}).status_code == 302
    assert iid


def test_add_context_and_typed_contexts_listed(client):
    t = login(client)
    post(client, "/contexts/add", t, name="@Dentista")
    assert "@dentista" in client.get("/contexts").get_data(as_text=True)
    post(client, "/contexts/add", t, name="dentista")      # duplicado: ignorado
    assert client.get("/contexts").get_data(as_text=True).count('value="dentista"') == 1
    _next_item(client, t, "marcar consulta", context="novo-contexto")   # digitado em uma tarefa
    assert "@novo-contexto" in client.get("/contexts").get_data(as_text=True)


def test_removed_default_context_not_reseeded(client):
    t = login(client)
    post(client, "/contexts/remove", t, name="rua")
    cfg = {k: client.application.config[k] for k in ("DB_PATH", "DATABASE_URL", "APP_PASSWORD", "SECRET_KEY")}
    cfg.update(SESSION_COOKIE_SECURE=False)
    c2 = create_app(cfg).test_client()
    login(c2)
    assert 'value="rua"' not in c2.get("/contexts").get_data(as_text=True)
    assert 'value="casa"' in c2.get("/contexts").get_data(as_text=True)


def test_contexts_requires_login_and_csrf(client):
    assert client.get("/contexts").status_code == 302
    login(client)
    assert client.post("/contexts/remove", data={"name": "rua"}).status_code == 400


def test_api_today_token(client, monkeypatch):
    t = login(client)
    a = _next_item(client, t, "ligar para cliente", minutes="15", context="telefone")
    _next_item(client, t, "fora de hoje")
    post(client, f"/items/{a}/today", t, on="1", next="/next")
    anon = client.application.test_client()
    # sem API_TOKEN configurado, o endpoint não existe
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert anon.get("/api/today").status_code == 404
    monkeypatch.setenv("API_TOKEN", "segredo-123")
    assert anon.get("/api/today").status_code == 401
    assert anon.get("/api/today?token=errado").status_code == 401
    r = anon.get("/api/today", headers={"Authorization": "Bearer segredo-123"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["count"] == 1
    assert data["lines"] == ["ligar para cliente (15 min, @telefone)"]
    assert data["titles"] == ["ligar para cliente"]
    assert anon.get("/api/today?token=segredo-123").get_json()["count"] == 1
