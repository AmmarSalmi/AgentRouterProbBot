# Run the bot on a Linux VPS

The VPS needs Docker Engine, Docker Compose V2, and a checkout of the bot. Use
`compose.vps.yaml` on the VPS. It uses the repository's `Dockerfile` and mounts
two folders next to the Compose file.

| Folder on the VPS | Path inside the container | Purpose |
| --- | --- | --- |
| `/opt/AgentRouterProbBot/data` | `/data` | Saved results and Telegram registration |
| `/opt/AgentRouterProbBot/codex` | `/codex` | Bot-managed Codex config |

## 1. Deploy the code

Place the checkout at `/opt/AgentRouterProbBot`. Stop any other instance
polling the same Telegram bot token before starting the VPS service.

## 2. Set Linux permissions and credentials

The VPS Compose file runs the bot as UID/GID `10001:10001`. The config sample
provides the AgentRouter model provider for the model picker.

```sh
cd /opt/AgentRouterProbBot
sudo install -d -m 700 -o 10001 -g 10001 data codex
sudo test -f codex/config.toml || sudo install -m 600 -o 10001 -g 10001 examples/codex-config.toml codex/config.toml
sudo chown -R 10001:10001 data codex
sudo chmod 700 data codex
sudo test -f .env.vps || sudo cp .env.vps.example .env.vps
sudo chmod 600 .env.vps
sudo nano .env.vps
```

Fill in the AgentRouter model key, dashboard access token, matching numeric
account ID, and Telegram bot token.

```dotenv
AGENTROUTER_API_KEY=sk-replace-me
AGENTROUTER_DASHBOARD_TOKEN=your-dashboard-access-token
AGENTROUTER_USER_ID=your-numeric-account-id
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=
```

Leave `TELEGRAM_CHAT_ID` blank to register your private chat through `/start`.
The model picker edits the Codex config mounted at `/codex/config.toml`. Use
the actual Codex config directory on your VPS if you want the picker to
control that installation.

## 3. Validate and start

```sh
cd /opt/AgentRouterProbBot
sudo docker compose --env-file .env.vps -f compose.vps.yaml config --quiet
sudo docker compose --env-file .env.vps -f compose.vps.yaml build
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --test-telegram
```

When the test waits for `/start`, open your bot's private chat and send
`/start`. After Telegram confirms registration, run one live cycle and start
the service.

```sh
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --once
sudo docker compose --env-file .env.vps -f compose.vps.yaml up -d
sudo docker compose --env-file .env.vps -f compose.vps.yaml logs --tail=80 prober
sudo docker compose --env-file .env.vps -f compose.vps.yaml ps
```

Use **Status**, **Balance & Usage**, and **Probe Now** in Telegram. Saved
probe results and registration are in `data/state.json` and `data/telegram.json`.

## Refresh the dashboard token

Update `AGENTROUTER_DASHBOARD_TOKEN` in `.env.vps` and recreate the service.

```sh
sudo docker compose --env-file .env.vps -f compose.vps.yaml up -d --force-recreate prober
```

## Troubleshooting

| Message | Action |
| --- | --- |
| `bind source path does not exist` | Create `data` and `codex` next to `compose.vps.yaml`. |
| `AgentRouter rejected dashboard authentication` | Check that `.env.vps` contains the raw access token and numeric ID for the same account. |
| Browser verification instead of model status | Check whether AgentRouter allows API requests from the VPS. |
| Telegram webhook or polling conflict | Stop other update listeners using the same Telegram token. |
| Permission error saving state or config | Check that `data` and `codex` are writable by UID/GID `10001:10001`. |
