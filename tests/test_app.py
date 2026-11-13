import re

import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app({"DB_PATH": str(tmp_path / "t.db"), "APP_PASSWORD": "pw",
                      "SECRET_KEY": "k", "SESSION_COOKIE_SECURE": False,
                      "TESTING": True})
    c = app.test_client()
    return c


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
        create_app({"DB_PATH": str(tmp_path / "a.db"), "APP_PASSWORD": "", "SECRET_KEY": ""})
