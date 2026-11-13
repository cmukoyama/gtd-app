# gtd-app

App web pessoal de gestão de tarefas com a metodologia GTD (Flask + SQLite, usuário único).

## Fluxo
- **Inbox**: capture várias linhas de uma vez; cada linha vira um item.
- **Esclarecer**: para cada item, escolha Fiz (<2min), Próxima ação (com @contexto, projeto, prazo), Aguardando, Algum dia ou Lixo.
- **Próximas ações**: filtradas por contexto.
- **Projetos**: cada projeto ativo deve ter ao menos uma próxima ação; os que não têm aparecem destacados.
- **Revisão semanal**: projetos parados, aguardando há mais de 7 dias, prazos vencidos.

## Rodar local
```bash
pip install -r requirements.txt
export APP_PASSWORD='sua-senha' SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')" INSECURE_COOKIES=1
python app.py            # http://localhost:8000
python -m pytest tests
```
`INSECURE_COOKIES=1` só para teste local em http; em produção use HTTPS e não defina.

## Hospedar (Docker)
```bash
docker build -t gtd-app .
docker run -p 8000:8000 -v gtd-data:/data \
  -e APP_PASSWORD=... -e SECRET_KEY=... gtd-app
```
O banco fica em `/data/gtd.db`: monte um volume persistente. Em Render, Fly.io ou Railway, defina `APP_PASSWORD` e `SECRET_KEY` como variáveis de ambiente e anexe um disco em `/data`.

O app recusa iniciar sem `APP_PASSWORD` e `SECRET_KEY`. Faça backup periódico do arquivo `gtd.db`.
