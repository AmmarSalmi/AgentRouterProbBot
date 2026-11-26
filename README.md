# AgentRouter Availability Prober

A Telegram bot that discovers the live AgentRouter model list from
`/api/user/model-status` and sends one small greeting probe to every returned
model through its advertised API. It reports model availability, account
balance, and total consumption.

For a Linux VPS, follow the [VPS setup guide](VPS_SETUP.md).

By default, a full discovery and probe cycle runs every 15 minutes, with at most
two model requests in parallel and a 32-token output limit per request. Model
requests run every cycle, including for models already confirmed available.
The watched list comes from model-status; documentation and pricing lists do
not determine which models are probed.

## Credentials and local files

You need:

1. `AGENTROUTER_API_KEY`: the normal `sk-...` key used for model requests.
   `CODEX_GATEWAY_API_KEY` is also accepted as a fallback.
2. `TELEGRAM_BOT_TOKEN`: a bot token created through Telegram's `@BotFather`.
3. `AGENTROUTER_DASHBOARD_TOKEN` and `AGENTROUTER_USER_ID`: the raw system
   access token generated in the AgentRouter dashboard and the matching
   numeric account ID. The bot sends them as `Authorization: Bearer <token>`
   and `New-Api-User: <account-id>` to read the balance and discover which
   models to probe.

Leave `TELEGRAM_CHAT_ID` blank or omit it. The first person who sends `/start`
in the bot's private chat becomes its saved recipient. An explicitly configured
chat ID skips registration.

## Start with Docker

The shared `compose.yaml` defaults to `.env` in the repository directory on
Windows and Linux. For a new install, copy `.env.example` to `.env` and fill in
the credentials above. If you already have `.env.vps`, rename it to `.env` or
copy its settings into your existing `.env`.

Prepare a writable `data/` directory, then run these commands from the
repository directory. [VPS setup](VPS_SETUP.md#2-set-linux-permissions-and-credentials)

```sh
docker compose build
docker compose run --rm prober --test-telegram
# When it says "Waiting for /start", open the bot and press Start.
docker compose run --rm prober --once
docker compose up -d
docker compose logs -f prober
```

`--test-telegram` needs only the Telegram configuration and sends a test message
without contacting AgentRouter. `--once` performs one full model cycle after
Telegram registration; it still sends availability notifications. It exits
non-zero when the status read fails or an authentication or protocol error
occurs during a probe or notification.

You can also start directly with `docker compose up -d --build`, then press
Start. A queued `/start` works. Stop registration or active requests with Ctrl+C
or `docker compose stop`; shutdown saves current state and pending alerts before
closing the HTTP clients.

Compose mounts `data/` at `/data` in the container. This directory preserves
`state.json` and `telegram.json` across restarts and `docker compose down`.

## Run directly on Windows

```powershell
pip install -e ".[test]"
$env:AGENTROUTER_API_KEY = "sk-replace-me"
$env:AGENTROUTER_DASHBOARD_TOKEN = "replace-me"
$env:AGENTROUTER_USER_ID = "12345"
$env:TELEGRAM_BOT_TOKEN = "123456789:replace-me"
$env:STATE_PATH = "$PWD\data\state.json"
agentrouter-prober
```

Direct execution reads environment variables from the shell.

## Telegram commands

Tap **Help** (`/help`) for a quick guide in Telegram. The bot probes AgentRouter
models automatically and sends alerts when availability changes or the
probing key is low on quota.

- **Status** (`/status`) shows the latest model availability results. A ✅ marks
  confirmed availability, and other results appear as ❌. Status waits for a
  running cycle, reads saved results, or starts a cycle when there are none.
- **Probe Now** (`/probe_now`) refreshes the model list and probes every listed
  model immediately.
- **Balance & Usage** (`/balance`) shows your balance, total usage, request count,
  and account group. The bot uses AgentRouter’s public `/api/status` settings
  to convert quota to dollars.
- **Probing Schedule** (`/probing_schedule`) sets automatic probes to run every
  5, 15, or 30 minutes or restores the default interval. Changes apply to the
  next automatic probe and persist across restarts. The default comes from
  `POLL_SECONDS`.
- **Credit Alerts** (`/credit_alerts`) sets one-time alerts for additional
  account spending or a low wallet balance.
- **API Keys** (`/api_keys`) lists each key’s remaining quota, total spent, and
  status. Select a key for 24-hour and 7-day usage reports, per-key alerts, and
  troubleshooting.
- **Weekly Digest** (`/weekly_digest`) compares the last two completed UTC
  weeks. View a report on demand or enable automatic delivery.

You can also use `/usage` for balance, `/probe` for Probe Now, and `/schedule`
for Probing Schedule.

Send `/start` to refresh the keyboard. The bot accepts commands from the
registered user in a private chat. An explicitly configured group or channel
can receive alerts. Command replies and alerts share Telegram rate limits.

## Credit alerts

Open **Credit Alerts**, choose a type, and enter a positive amount in the units
shown by **Balance & Usage**. Use up to two decimal places for dollars or a whole
number for quota units.

- **Spending Alert** adds the amount to your total usage at setup. The bot
  confirms your current total and the total that will trigger the alert.
- **Low Balance Alert** fires when your live balance falls below the amount
  you set. The bot uses your current balance to estimate the additional spend
  and the total usage at that point. Top-ups change that estimate.

The bot checks credit alerts during each full probe cycle. It saves one alert
of each type across restarts. Setting another alert of the same type replaces
the previous one. An alert clears after Telegram accepts it.

The bot also checks the API key used for model probes. It reads the key's
remaining quota from the dashboard's key list and sends a Telegram warning
when a limited key falls below $1.00 by default. Set `PROBE_KEY_LOW_BALANCE_USD`
to change the threshold. The warning fires once per low-quota period and
rearms when the quota recovers to the threshold. Unlimited keys have no
per-key balance to monitor.

## API key monitoring

Open **API Keys** to see the shared account balance alongside the quota and
lifetime spending for each key. A key’s quota is its individual spending cap
on the shared account balance. Keys without a cap show **Unlimited**. The bot
reads these fields with the dashboard token and uses numeric key IDs to keep
keys with duplicate names or renamed keys distinct. It shows key metadata in
the registered private chat.

- **Last 24 Hours** and **Last 7 Days** show spend, logged request counts, and
  the top models by spend for that key. The reports use AgentRouter’s available
  consumption logs.
- **Key Alerts** lets you set a one-time alert for additional spending, quota
  below a threshold, or a set number of days before expiry. Setting the same
  alert again replaces it. **Clear Key Alerts** clears the selected key’s
  custom alerts. The automatic probing-key warning runs independently.
- **Troubleshoot Key** shows whether the key is disabled, expired, or out of
  quota, along with its group and model and IP restrictions. It also displays
  the latest model results from the bot’s probing key.

**Weekly Digest** reports total logged spend, the change versus the prior
week, and the top five keys and models by spend. Automatic delivery is off by
default. Enable it from the menu to get one digest after each completed UTC
week (Monday through Sunday). The bot checks delivery on its regular probe
schedule. Alerts and the digest subscription persist across restarts and use
the existing Telegram retry handling.

## Registration

Registration is tied to the bot's numeric identity, so rotating its token keeps
the recipient. Configuring a different bot requires a new `/start`. Run one
update listener per bot; a webhook or another polling instance causes Telegram
conflicts. Temporary connection failures use capped retry delays and honor
Telegram's requested rate-limit delay.

To choose another recipient, stop the service, remove its Telegram registration,
and restart with `TELEGRAM_CHAT_ID` blank:

```sh
docker compose stop prober
docker compose run --rm --entrypoint python prober -c "from pathlib import Path; import os; Path(os.getenv('STATE_PATH', '/data/state.json')).with_name('telegram.json').unlink(missing_ok=True)"
docker compose up -d prober
```

Then send `/start` from the intended private chat. With a custom `STATE_PATH`,
registration is stored as `telegram.json` in the same directory. Unreadable or
corrupt registration produces an error.

## Availability behavior

- Dashboard status and metrics are saved for diagnostics. Every listed model is
  probed, including models marked `degraded`, `down`, or `no_traffic`.
- API advertisements determine the request: `responses` uses `/v1/responses`,
  `chat` uses `/v1/chat/completions`, and `claude_native` uses `/v1/messages`.
  Responses is preferred when a model advertises multiple supported APIs.
- Confirmation requires a valid API response with generated output. A
  Responses request stopped by its output-token limit also confirms
  availability when it contains output or reports generated tokens.
- HTTP 402 pool exhaustion, HTTP 503, and recognized no-channel/model-not-found
  responses mean unavailable. Timeouts, rate limits, and malformed replies are
  recorded as probe errors instead of being guessed as unavailable.
- Changes in confirmed availability produce one compact list for all live
  models, grouped under **Available** and **Not available**. The list contains
  model names only; failed or unconfirmed checks appear under **Unknown**.
  Unchanged results do not repeat the notification.
- Pending lists survive shutdown. Telegram rate limits pause delivery until
  their saved retry deadline; delivery resumes on a later cycle.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTROUTER_BASE_URL` | `https://agentrouter.org` | Status and model API host |
| `TELEGRAM_CHAT_ID` | empty | Explicit destination; otherwise register via Start |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Telegram API host |
| `AGENTROUTER_DASHBOARD_TOKEN` | required | Dashboard access token for balance, model discovery, API key metadata, and usage logs |
| `AGENTROUTER_USER_ID` | required | Positive account ID paired with the dashboard token |
| `POLL_SECONDS` | `900` | Default interval between automatic probe cycles |
| `PROBE_MAX_OUTPUT_TOKENS` | `32` | Output-token limit for each greeting |
| `PROBE_REASONING_EFFORT` | `low` | `minimal`, `low`, `medium`, or `high` for Responses |
| `PROBE_CONCURRENCY` | `2` | Maximum simultaneous model probes |
| `PROBE_KEY_LOW_BALANCE_USD` | `1.00` | Warn when the probing key's remaining quota falls below this USD amount |
| `REQUEST_TIMEOUT_SECONDS` | `90` | HTTP timeout |
| `STATE_PATH` | `/data/state.json` | Saved probe results, interval, alerts, and weekly digest subscription. Registration uses the same directory. |
| `HEALTH_MAX_AGE_SECONDS` | `3600` | Maximum age of a successful model-status read |
| `AGENTROUTER_CLIENT_USER_AGENT` | `codex_cli_rs/0.171.0 (Windows 11; x86_64)` | Model client user agent |
| `LOG_LEVEL` | `INFO` | Log level |

`NORMAL_POLL_SECONDS` and `CONFIRM_MAX_OUTPUT_TOKENS` remain fallbacks for
`POLL_SECONDS` and `PROBE_MAX_OUTPUT_TOKENS`, respectively.

Docker's health check runs `--healthcheck`, which checks the age of the saved
successful model-status read. It needs no credentials and exposes no HTTP
port. Health does not mean every model is available.

## Tests

```sh
pip install -e ".[test]"
pytest tests/
```

The tests run offline with mock HTTP transports.
