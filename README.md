# gtd-app

App web pessoal de gestão de tarefas com a metodologia GTD (Flask + SQLite, usuário único).

## Fluxo
- **Inbox**: capture várias linhas de uma vez; cada linha vira um item.
- **Esclarecer**: para cada item, escolha Fiz (<2min), Próxima ação (com @contexto, projeto, prazo), Aguardando, Algum dia ou Lixo.
- **Próximas ações**: filtradas por contexto.
- **Projetos**: cada projeto ativo deve ter ao menos uma próxima ação; os que não têm aparecem destacados.
- **Revisão semanal**: projetos parados, aguardando há mais de 7 dias, prazos vencidos.

## Rodar local
Sem `DATABASE_URL` o app usa SQLite (`gtd.db`), bom para desenvolvimento.
```bash
pip install -r requirements.txt pytest
export APP_PASSWORD='sua-senha' SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')" INSECURE_COOKIES=1
python app.py            # http://localhost:8000
python -m pytest tests
```
`INSECURE_COOKIES=1` só para teste local em http; em produção (https) não defina.

Para testar também contra Postgres, aponte para um banco **de teste** (os testes apagam as tabelas):
`TEST_DATABASE_URL=postgresql://... python -m pytest tests`

## Deploy na Vercel + Neon
1. **Neon**: crie um projeto (ou uma branch nova) e copie a connection string **com pooler** (`...-pooler...`).
2. **Vercel**: *Add New → Project* → importe `gtd-app` (framework: Flask, detectado pelo `app.py`).
3. Em *Settings → Environment Variables*, defina:
   - `DATABASE_URL`: a string do Neon;
   - `APP_PASSWORD`: a senha de acesso;
   - `SECRET_KEY`: string aleatória longa (`python -c "import secrets;print(secrets.token_hex(32))"`).
4. Faça o deploy. As tabelas são criadas automaticamente no primeiro acesso.

O app recusa iniciar sem `APP_PASSWORD` e `SECRET_KEY`. Use um banco/branch do Neon só para este app.

## Tarefas de "Hoje" na tela de bloqueio do iPhone

1. No Vercel, crie a variável `API_TOKEN` (texto longo e aleatório) e faça redeploy.
2. `GET /api/today` devolve JSON com as tarefas de Hoje (inclui as arrastadas de dias
   anteriores). Envie o token no cabeçalho `Authorization: Bearer <token>`
   (ou `?token=<token>`, menos seguro por poder aparecer em logs).
   Sem `API_TOKEN` o endpoint responde 404.
3. Um Atalho do iPhone lê esse JSON e recria a lista "Hoje" no app Lembretes, que
   pode ser exibida pelo widget de Lembretes na tela de bloqueio.

## Assistente no Telegram

O bot lê e altera as tarefas do GTD, faz check-ins de hora em hora (8h30–17h30, dias úteis)
e conversa via Claude quando `ANTHROPIC_API_KEY` está definida. Código em `bot.py`.

1. No Telegram, fale com **@BotFather** → `/newbot` → guarde o token.
2. No Vercel, defina as variáveis e faça redeploy:
   - `TELEGRAM_BOT_TOKEN`: token do BotFather;
   - `TELEGRAM_WEBHOOK_SECRET` e `CRON_SECRET`: textos aleatórios longos (só letras, números, `_` e `-`);
   - `ANTHROPIC_API_KEY`: chave da API do Claude (opcional; sem ela só comandos e botões);
   - `TELEGRAM_CHAT_ID`: deixe vazio por enquanto.
3. Abra `https://SEU-APP/telegram/setup?token=CRON_SECRET` para registrar o webhook.
4. Mande `/start` para o bot: ele responde o seu chat id. Coloque em `TELEGRAM_CHAT_ID` e faça redeploy.
   O bot só atende esse chat.
5. Em cron-job.org, crie um job `GET https://SEU-APP/telegram/tick` com o cabeçalho
   `Authorization: Bearer CRON_SECRET`, fuso `America/Sao_Paulo`, nos minutos 30 das 8h às 17h,
   de segunda a sexta. O próprio endpoint decide: 8h30 plano do dia, 17h30 fechamento, demais check-in.

Comandos: `/hoje` (lista com botões ✅ e ➡️ amanhã), `/feito 12`, `/inbox texto`.
Texto livre vai para o Claude, que usa as ferramentas do GTD (listar, criar, concluir, planejar o dia).

### Relatórios

- **Vértice (9h)**: defina `VERTICE_URL` (ex.: `https://vertice-dranataliamedeiros.vercel.app`) e
  `VERTICE_BOT_TOKEN` (o mesmo valor de `BOT_API_TOKEN` no Vértice). No cron-job.org, crie um job
  `GET /telegram/relatorio/vertice` às 9h00, dias úteis, com o mesmo cabeçalho `Authorization: Bearer CRON_SECRET`.
- **Encaminhar texto pronto**: `POST /telegram/encaminhar` com `{"texto": "..."}` e o mesmo cabeçalho.
