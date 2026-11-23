from datetime import datetime

import pytest

from app import create_app


class FakeHttp:
    def __init__(self, claude_replies=None):
        self.calls = []
        self.claude = list(claude_replies or [])

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload))
        if "anthropic" in url:
            return self.claude.pop(0)
        return {"ok": True}

    def sent(self):
        return [p["text"] for u, p in self.calls if u.endswith("/sendMessage")]


@pytest.fixture
def make(tmp_path):
    def _make(api_key="", replies=None):
        http = FakeHttp(replies)
        app = create_app({"DB_PATH": str(tmp_path / "t.db"), "DATABASE_URL": "",
                          "APP_PASSWORD": "pw", "SECRET_KEY": "k", "TESTING": True,
                          "TELEGRAM_BOT_TOKEN": "bt", "TELEGRAM_CHAT_ID": "42",
                          "TELEGRAM_WEBHOOK_SECRET": "ws", "CRON_SECRET": "cs",
                          "ANTHROPIC_API_KEY": api_key, "BOT_HTTP": http})
        return app, app.test_client(), http
    return _make


def hook(client, text=None, chat=42, cb=None, secret="ws"):
    upd = {"callback_query": cb} if cb else {"message": {"chat": {"id": chat}, "text": text}}
    return client.post("/telegram/webhook", json=upd,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def add_today(app, title="Escrever relatório"):
    from datetime import date
    with app.app_context():
        import sqlite3
        c = sqlite3.connect(app.config["DB_PATH"])
        c.execute("INSERT INTO items (title, status, focus_on, minutes, created_at) "
                  "VALUES (?, 'next', ?, 25, '2026-01-01')", (title, date.today().isoformat()))
        c.commit()


def test_rejects_bad_secret(make):
    _, c, _ = make()
    assert hook(c, "/hoje", secret="x").status_code == 401


def test_unknown_chat_only_gets_its_id(make):
    _, c, http = make()
    hook(c, "/start", chat=7)
    assert http.calls[-1][1] == {"chat_id": 7, "text": "Seu chat id: 7"}


def test_inbox_and_hoje_and_done_button(make):
    app, c, http = make()
    c.get("/telegram/tick?token=cs&force=manha")   # cria o schema
    hook(c, "/inbox Ligar para o banco")
    assert "No Inbox" in http.sent()[-1]
    add_today(app)
    hook(c, "/hoje")
    assert "Escrever relatório" in http.sent()[-1]
    item_id = 2
    hook(c, cb={"id": "q", "data": f"done:{item_id}", "message": {"chat": {"id": 42}}})
    assert "concluída" in http.sent()[-1]
    hook(c, f"/feito {item_id}")
    assert "Não achei" in http.sent()[-1]


def test_free_text_without_key_goes_to_inbox(make):
    _, c, http = make()
    hook(c, "comprar presente da Sofia")
    assert "Inbox" in http.sent()[-1]


def test_claude_tool_loop(make):
    replies = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "criar_acao",
             "input": {"titulo": "Revisar PR", "hoje": True, "minutos": 15}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Criada #1. Começa agora: 15 min."}]},
    ]
    app, c, http = make(api_key="k", replies=replies)
    hook(c, "coloca revisar PR pra hoje, 15 min")
    assert http.sent()[-1] == "Criada #1. Começa agora: 15 min."
    hook(c, "/hoje")
    assert "Revisar PR" in http.sent()[-1]


def test_tick_requires_token_and_window(make):
    app, c, http = make()
    assert c.get("/telegram/tick").status_code == 401
    slot = app.bot_slot
    assert slot(datetime(2026, 11, 23, 8, 30)) == "manha"
    assert slot(datetime(2026, 11, 23, 12, 30)) == "checkin"
    assert slot(datetime(2026, 11, 23, 17, 30)) == "fechamento"
    assert slot(datetime(2026, 11, 23, 19, 30)) is None
    assert slot(datetime(2026, 11, 22, 10, 30)) is None   # domingo
    r = c.get("/telegram/tick?token=cs&force=checkin").get_json()
    assert r["sent"] and "Nada em Hoje" in http.sent()[-1]


def test_relatorio_vertice_repassa_texto(make, tmp_path):
    app, c, http = make()
    app.config.update(VERTICE_URL="https://v", VERTICE_BOT_TOKEN="vt")

    def fake(url, payload, headers):
        http.calls.append((url, payload))
        if url == "https://v/api/cron/resumo-bot":
            assert headers["Authorization"] == "Bearer vt"
            return {"texto": "📊 Vértice — resumo do dia"}
        return {"ok": True}
    app.config["BOT_HTTP"] = fake
    assert c.get("/telegram/relatorio/vertice").status_code == 401
    assert c.get("/telegram/relatorio/vertice?token=cs").get_json()["sent"]
    assert http.sent()[-1] == "📊 Vértice — resumo do dia"


def test_encaminhar_divide_texto_longo(make):
    _, c, http = make()
    r = c.post("/telegram/encaminhar?token=cs", json={"texto": "a" * 4500})
    assert r.status_code == 200
    assert [len(t) for t in http.sent()] == [4000, 500]
