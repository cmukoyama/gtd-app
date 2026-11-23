"""Assistente pessoal no Telegram ligado ao GTD.

Rotas:
  POST /telegram/webhook        recebe mensagens do Telegram (secret_token no cabeçalho)
  GET  /telegram/setup?token=   registra o webhook no Telegram (usa CRON_SECRET)
  GET  /telegram/tick?token=    chamado de hora em hora (8h30..17h30, dias úteis):
                                8h30 = plano do dia, 17h30 = fechamento, resto = check-in

Variáveis de ambiente:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET, CRON_SECRET,
  ANTHROPIC_API_KEY (opcional; sem ela o bot funciona só com comandos e botões),
  ANTHROPIC_MODEL (padrão claude-sonnet-5-5)
"""
import hmac
import json
import os
import urllib.request
from datetime import date, datetime, timedelta

from flask import abort, jsonify, request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
HISTORY = 16          # mensagens de contexto enviadas ao modelo
MAX_TOOL_ROUNDS = 6

BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_messages (
    id {pk},
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

SYSTEM = """Você é o assistente pessoal do Carlos no Telegram, ligado ao app GTD dele.
Carlos tem TDAH e pediu ajuda para atravessar o dia. Seu jeito:
- Mensagens curtas, em português, sem preâmbulo nem frases de encerramento.
- Comece pela próxima ação. Uma coisa de cada vez.
- Tarefa travada: quebre em passos de no máximo 10 minutos e diga só o primeiro.
- Listas numeradas, no máximo 5 itens.
- Estimativas em minutos concretos.
- Comemore o que foi concluído em uma linha, sem exagero.
- Assunto paralelo que aparecer: capture no Inbox e volte ao foco.
- Sem julgamento quando algo atrasar; proponha replanejar.
Use as ferramentas para ler e alterar as tarefas; nunca invente tarefas ou ids.
Itens de "Hoje" são próximas ações marcadas para hoje (inclui as arrastadas de dias anteriores).
Agora: {agora} ({dia_semana})."""

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

TOOLS = [
    {"name": "listar_hoje", "description": "Lista as tarefas de Hoje (id, título, minutos, contexto, prazo).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "listar_proximas", "description": "Lista próximas ações; filtra por contexto se informado.",
     "input_schema": {"type": "object", "properties": {"contexto": {"type": "string"}}}},
    {"name": "capturar_inbox", "description": "Joga itens no Inbox para esclarecer depois.",
     "input_schema": {"type": "object", "required": ["titulos"],
                      "properties": {"titulos": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "criar_acao", "description": "Cria uma próxima ação já esclarecida.",
     "input_schema": {"type": "object", "required": ["titulo"], "properties": {
         "titulo": {"type": "string"}, "contexto": {"type": "string"},
         "minutos": {"type": "integer"}, "prazo": {"type": "string", "description": "AAAA-MM-DD"},
         "hoje": {"type": "boolean"}}}},
    {"name": "concluir", "description": "Marca uma tarefa como feita.",
     "input_schema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
    {"name": "planejar_dia", "description": "Coloca a tarefa em Hoje, em outra data (AAAA-MM-DD) ou tira do dia (data vazia).",
     "input_schema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "integer"}, "data": {"type": "string"}}}},
]


def register_bot(app, db, local_today, now, ctx_list, tz):
    cfg = app.config
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_WEBHOOK_SECRET",
              "CRON_SECRET", "ANTHROPIC_API_KEY"):
        cfg.setdefault(k, os.environ.get(k, ""))
    cfg.setdefault("ANTHROPIC_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    ready = {"ok": False}

    # ---------- infraestrutura ----------
    def http_json(url, payload, headers=None):
        fake = cfg.get("BOT_HTTP")   # testes injetam um fake
        if fake:
            return fake(url, payload, headers or {})
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=50) as r:
            return json.loads(r.read())

    def tg(method, **payload):
        return http_json(f"https://api.telegram.org/bot{cfg['TELEGRAM_BOT_TOKEN']}/{method}", payload)

    def send(text, buttons=None):
        payload = {"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": text[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return tg("sendMessage", **payload)

    def conn():
        d = db()
        if not ready["ok"]:
            pk = "SERIAL PRIMARY KEY" if d.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
            d.execute(BOT_SCHEMA.format(pk=pk).strip())
            d.commit()
            ready["ok"] = True
        return d

    def check_secret(sent, expected):
        if not expected or not hmac.compare_digest((sent or "").encode(), expected.encode()):
            abort(401)

    def cron_auth():
        auth = request.headers.get("Authorization", "")
        sent = auth[7:] if auth.startswith("Bearer ") else request.args.get("token", "")
        check_secret(sent, cfg["CRON_SECRET"])

    # ---------- tarefas ----------
    def today_items():
        t = local_today().isoformat()
        rows = conn().execute(
            """SELECT i.*, p.title AS project_title FROM items i
               LEFT JOIN projects p ON p.id = i.project_id
               WHERE i.status='next' AND i.focus_on IS NOT NULL AND i.focus_on <= ?
               ORDER BY (i.focus_on < ?), (i.due IS NULL), i.due, i.id""", (t, t)).fetchall()
        return [dict(r) for r in rows]

    def fmt(i):
        bits = []
        if i.get("minutes"):
            bits.append(f"{i['minutes']} min")
        if i.get("context"):
            bits.append(" ".join("@" + c for c in ctx_list(i["context"])))
        if i.get("due"):
            bits.append(f"prazo {i['due']}")
        return f"#{i['id']} {i['title']}" + (f" ({', '.join(bits)})" if bits else "")

    def task_buttons(items):
        return [[{"text": f"✅ #{i['id']}", "callback_data": f"done:{i['id']}"},
                 {"text": "➡️ amanhã", "callback_data": f"tomorrow:{i['id']}"}]
                for i in items[:5]]

    def complete(item_id):
        cur = conn().execute("UPDATE items SET status='done', completed_at=? WHERE id=? AND status!='done'",
                             (now(), item_id))
        conn().commit()
        return cur.rowcount == 1

    def set_day(item_id, day):
        cur = conn().execute("UPDATE items SET focus_on=? WHERE id=? AND status='next'", (day, item_id))
        conn().commit()
        return cur.rowcount == 1

    def capture(titles):
        ids = [conn().insert("INSERT INTO items (title, status, created_at) VALUES (?, 'inbox', ?)",
                             (t.strip()[:500], now())) for t in titles if t.strip()]
        conn().commit()
        return ids

    def run_tool(name, args):
        if name == "listar_hoje":
            return [fmt(i) for i in today_items()] or "Nada marcado para hoje."
        if name == "listar_proximas":
            sql, params = "SELECT * FROM items WHERE status='next'", []
            if args.get("contexto"):
                sql += " AND (',' || context || ',') LIKE ?"
                params.append(f"%,{args['contexto'].lstrip('@').lower()},%")
            rows = conn().execute(sql + " ORDER BY (due IS NULL), due, id LIMIT 30", params).fetchall()
            return [fmt(dict(r)) for r in rows] or "Nenhuma próxima ação."
        if name == "capturar_inbox":
            return {"ids": capture(args.get("titulos") or [])}
        if name == "criar_acao":
            new_id = conn().insert(
                "INSERT INTO items (title, status, context, minutes, due, focus_on, created_at) "
                "VALUES (?, 'next', ?, ?, ?, ?, ?)",
                (args["titulo"][:500], (args.get("contexto") or "").lstrip("@").lower(),
                 args.get("minutos"), args.get("prazo") or None,
                 local_today().isoformat() if args.get("hoje") else None, now()))
            conn().commit()
            return {"id": new_id}
        if name == "concluir":
            return {"ok": complete(int(args["id"]))}
        if name == "planejar_dia":
            day = args.get("data") or None
            if day == "hoje":
                day = local_today().isoformat()
            if day:
                date.fromisoformat(day)
            return {"ok": set_day(int(args["id"]), day)}
        return {"erro": f"ferramenta desconhecida: {name}"}

    # ---------- memória curta da conversa ----------
    def remember(role, text):
        conn().execute("INSERT INTO bot_messages (role, content, created_at) VALUES (?, ?, ?)",
                       (role, text, now()))
        conn().commit()

    def history():
        since = (datetime.now(tz) - timedelta(hours=12)).replace(tzinfo=None).isoformat()
        rows = conn().execute(
            "SELECT role, content FROM bot_messages WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (since, HISTORY)).fetchall()
        msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while msgs and msgs[0]["role"] != "user":   # a API exige começar pelo usuário
            msgs.pop(0)
        merged = []                                   # e alternar papéis
        for m in msgs:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(m)
        return merged

    def ask_claude(user_text):
        """Uma volta de conversa com ferramentas. Devolve o texto final."""
        n = datetime.now(tz)
        system = SYSTEM.format(agora=n.strftime("%Y-%m-%d %H:%M"), dia_semana=DIAS[n.weekday()])
        remember("user", user_text)
        messages = history()
        headers = {"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}
        text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = http_json(ANTHROPIC_URL, {"model": cfg["ANTHROPIC_MODEL"], "max_tokens": 1024,
                                             "system": system, "tools": TOOLS, "messages": messages},
                             headers)
            blocks = resp.get("content", [])
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text").strip()
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not uses:
                break
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for u in uses:
                try:
                    out = run_tool(u["name"], u.get("input") or {})
                except Exception as e:  # erro vira resposta para o modelo corrigir
                    out = {"erro": str(e)}
                results.append({"type": "tool_result", "tool_use_id": u["id"],
                                "content": json.dumps(out, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        text = text or "Feito."
        remember("assistant", text)
        return text

    # ---------- mensagens do Telegram ----------
    HELP = ("Comandos:\n/hoje - tarefas de hoje com botões\n/feito 12 - conclui a #12\n"
            "/inbox texto - captura no Inbox\nOu escreva normalmente que eu entendo.")

    def handle_text(text):
        text = (text or "").strip()
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/ajuda", "/help"):
            return send(HELP)
        if cmd == "/hoje":
            items = today_items()
            if not items:
                return send("Nada marcado para hoje. Quer que eu sugira 3 das próximas ações?")
            return send("Hoje:\n" + "\n".join(f"{n}. {fmt(i)}" for n, i in enumerate(items, 1)),
                        task_buttons(items))
        if cmd == "/feito" and rest.strip().lstrip("#").isdigit():
            ok = complete(int(rest.strip().lstrip("#")))
            return send("✅ Feito." if ok else "Não achei essa tarefa aberta.")
        if cmd == "/inbox" and rest.strip():
            capture(rest.split("\n"))
            return send("📥 No Inbox.")
        if not cfg["ANTHROPIC_API_KEY"]:
            capture([text])
            return send("📥 Guardei no Inbox. (Para conversar, defina ANTHROPIC_API_KEY.)")
        return send(ask_claude(text))

    def handle_callback(cb):
        action, _, raw = (cb.get("data") or "").partition(":")
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        if not raw.isdigit():
            return
        item_id = int(raw)
        if action == "done":
            ok = complete(item_id)
            send(f"✅ #{item_id} concluída. Próxima: /hoje" if ok else "Essa já estava fechada.")
        elif action == "tomorrow":
            ok = set_day(item_id, (local_today() + timedelta(days=1)).isoformat())
            send(f"➡️ #{item_id} foi para amanhã." if ok else "Não consegui mover essa.")

    @app.post("/telegram/webhook")
    def telegram_webhook():
        check_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
                     cfg["TELEGRAM_WEBHOOK_SECRET"])
        upd = request.get_json(silent=True) or {}
        msg = upd.get("message") or {}
        cb = upd.get("callback_query") or {}
        chat = (msg.get("chat") or (cb.get("message") or {}).get("chat") or {}).get("id")
        if str(chat) != str(cfg["TELEGRAM_CHAT_ID"]):
            # desconhecido: só responde o próprio chat id (útil na configuração)
            if chat and msg.get("text", "").startswith("/start"):
                tg("sendMessage", chat_id=chat, text=f"Seu chat id: {chat}")
            return jsonify(ok=True)
        try:
            if cb:
                handle_callback(cb)
            elif msg.get("text"):
                handle_text(msg["text"])
        except Exception as e:  # responde 200 para o Telegram não reenviar em loop
            app.logger.exception("erro no bot")
            try:
                send(f"⚠️ Deu erro aqui: {type(e).__name__}. Tente de novo em 1 min.")
            except Exception:
                pass
        return jsonify(ok=True)

    @app.get("/telegram/setup")
    def telegram_setup():
        cron_auth()
        url = request.url_root.replace("http://", "https://") + "telegram/webhook"
        r = tg("setWebhook", url=url, secret_token=cfg["TELEGRAM_WEBHOOK_SECRET"],
               allowed_updates=["message", "callback_query"])
        return jsonify(webhook=url, telegram=r)

    # ---------- lembretes agendados ----------
    def slot(n):
        """manha | fechamento | checkin | None (fora da janela 8h30-17h30, dias úteis)."""
        if n.weekday() >= 5:
            return None
        minutes = n.hour * 60 + n.minute
        if not (8 * 60 + 20 <= minutes <= 17 * 60 + 45):
            return None
        if minutes < 9 * 60:
            return "manha"
        if minutes >= 17 * 60 + 20:
            return "fechamento"
        return "checkin"

    PROMPTS = {
        "manha": "[lembrete automático 8h30] Monte meu plano do dia: liste as tarefas de Hoje, "
                 "diga qual começar agora e quanto tempo leva. Se não houver nada em Hoje, "
                 "sugira até 3 próximas ações.",
        "checkin": "[check-in automático de hora em hora] Pergunte em uma linha em que estou, "
                   "lembre a próxima tarefa de Hoje e ofereça quebrá-la se estiver travada.",
        "fechamento": "[fechamento automático 17h30] Faça o fechamento do dia: o que foi concluído "
                      "hoje, o que ficou aberto em Hoje e pergunte o que vai para amanhã.",
    }

    def fallback(kind):
        items = today_items()
        if kind == "fechamento":
            done = conn().execute("SELECT COUNT(*) c FROM items WHERE status='done' AND completed_at >= ?",
                                  (local_today().isoformat(),)).fetchone()["c"]
            return f"Fechamento: {done} concluída(s) hoje, {len(items)} aberta(s) em Hoje. O que vai para amanhã?"
        if not items:
            return "Nada em Hoje. Escolha 1 próxima ação e me diga qual."
        head = "Bom dia! Plano de hoje:" if kind == "manha" else "Check-in: em que você está? Próxima:"
        return head + "\n" + "\n".join(f"{n}. {fmt(i)}" for n, i in enumerate(items[:5], 1))

    @app.route("/telegram/tick", methods=["GET", "POST"])
    def telegram_tick():
        cron_auth()
        kind = request.args.get("force") or slot(datetime.now(tz))
        if kind not in PROMPTS:
            return jsonify(sent=False, reason="fora da janela")
        if cfg["ANTHROPIC_API_KEY"]:
            text = ask_claude(PROMPTS[kind])
        else:
            text = fallback(kind)
        send(text, task_buttons(today_items()) if kind != "fechamento" else None)
        return jsonify(sent=True, kind=kind)

    app.bot_slot = slot   # exposto para testes
