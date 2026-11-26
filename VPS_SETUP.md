# Run the bot on a Linux VPS

The VPS needs Docker Engine, Docker Compose V2, and a checkout of the bot.
`compose.yaml` mounts the adjacent `data/` directory at `/data` in the
container to store probe results, Telegram registration, and verified
AgentRouter credentials.

## 1. Deploy the code

Place the checkout at `/opt/AgentRouterProbBot`. Stop any other instance
polling the same Telegram bot token before starting the VPS service.

## 2. Set Linux permissions and credentials

The Compose service runs the bot as UID/GID `10001:10001`.

If you already have `.env.vps`, rename it to `.env` or copy its settings into
your existing `.env` before running the commands below.

```sh
cd /opt/AgentRouterProbBot
sudo install -d -m 700 -o 10001 -g 10001 data
sudo chown -R 10001:10001 data
sudo chmod 700 data
sudo test -f .env || sudo install -m 600 .env.example .env
sudo chmod 600 .env
sudo nano .env
```

Fill in the Telegram bot token. Leave the AgentRouter values blank to enter
them in the bot's private chat, or prefill any you already have.

```dotenv
AGENTROUTER_API_KEY=
AGENTROUTER_DASHBOARD_TOKEN=
AGENTROUTER_USER_ID=
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=
```

Leave `TELEGRAM_CHAT_ID` blank to register your private chat through `/start`.

## 3. Validate and start

```sh
cd /opt/AgentRouterProbBot
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose logs --tail=80 prober
sudo docker compose ps
```

Send `/start` in the bot's private chat, then tap **Status** or **Balance** to
provide any missing credentials. The bot starts probing after validation and
saves the verified values in `/data/credentials/.env` inside the container.

## Refresh the dashboard token

Update `AGENTROUTER_DASHBOARD_TOKEN` in `data/credentials/.env` and recreate the
service. If setup has yet to save credentials, use the repository's `.env`.

```sh
sudo docker compose up -d --force-recreate prober
```

## Troubleshooting

| Message | Action |
| --- | --- |
| `bind source path does not exist` | Create `data` next to `compose.yaml`. |
| `AgentRouter rejected the dashboard token and account ID pair` | Send the matching ID and raw dashboard token in the bot's setup prompts. |
| Browser verification instead of model status | Check whether AgentRouter allows API requests from the VPS. |
| Telegram webhook or polling conflict | Stop other update listeners using the same Telegram token. |
| Permission error saving state | Check that `data` is writable by UID/GID `10001:10001`. |
