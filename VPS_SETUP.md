# Run the bot on a Linux VPS

The VPS needs Docker Engine, Docker Compose V2, and a checkout of the bot.
`compose.yaml` mounts the adjacent `data/` directory at `/data` in the
container to store probe results and Telegram registration.

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

## 3. Validate and start

```sh
cd /opt/AgentRouterProbBot
sudo docker compose config --quiet
sudo docker compose build
sudo docker compose run --rm prober --test-telegram
```

When the test waits for `/start`, open your bot's private chat and send
`/start`. After Telegram confirms registration, run one live cycle and start
the service.

```sh
sudo docker compose run --rm prober --once
sudo docker compose up -d
sudo docker compose logs --tail=80 prober
sudo docker compose ps
```

Use **Status**, **Balance**, and **Probe Now** in Telegram. Saved
probe results and registration are in `data/state.json` and `data/telegram.json`.

## Refresh the dashboard token

Update `AGENTROUTER_DASHBOARD_TOKEN` in `.env` and recreate the service.

```sh
sudo docker compose up -d --force-recreate prober
```

## Troubleshooting

| Message | Action |
| --- | --- |
| `bind source path does not exist` | Create `data` next to `compose.yaml`. |
| `AgentRouter rejected dashboard authentication` | Check that `.env` contains the raw access token and numeric ID for the same account. |
| Browser verification instead of model status | Check whether AgentRouter allows API requests from the VPS. |
| Telegram webhook or polling conflict | Stop other update listeners using the same Telegram token. |
| Permission error saving state | Check that `data` is writable by UID/GID `10001:10001`. |
