# AgentRouter Availability Prober

A Telegram bot that discovers the live AgentRouter model list from
`/api/user/model-status` and sends one small greeting probe to every returned
model through its advertised API. It reports model availability, account
balance, and total consumption.

[Docker setup for Windows and Linux](#start-with-docker)

By default, a full discovery and probe cycle runs every 15 minutes, with at most
two model requests in parallel and a 32-token output limit per request. Model
requests run every cycle, including for models already confirmed available.
The watched list comes from model-status; documentation and pricing lists do
not determine which models are probed.

## AgentRouter credentials

Both setup methods require `TELEGRAM_BOT_TOKEN`, which you get from Telegram's
`@BotFather`. It lets the bot receive commands and send replies and alerts.

- `AGENTROUTER_API_KEY` is the model key (`sk-...`). It authorizes the small
  requests the bot sends to check whether each model responds.
- `AGENTROUTER_DASHBOARD_TOKEN` is the raw system access token generated in the
  AgentRouter dashboard. It lets the bot discover which models to probe and
  read your balance, API key quotas, and usage logs for reports and alerts.
- `AGENTROUTER_USER_ID` is the numeric ID of the account that generated the
  dashboard token. AgentRouter checks the two together on dashboard requests.

### Find your account ID

Open **Personal Settings** and copy the digits next to **ID** on your profile
card. [AgentRouter console](https://agentrouter.org/console)

![AgentRouter console with Personal Settings selected and the account ID circled](src/agentrouter_prober/assets/agentrouter-account-id.png)

### Generate a dashboard token

Open **Personal Settings** → **Security Settings**. Under **System Access
Token**, select **Generate Token** and copy the raw token for
`AGENTROUTER_DASHBOARD_TOKEN`. [AgentRouter console](https://agentrouter.org/console)

![AgentRouter Security Settings showing the Generate Token button for the System Access Token](src/agentrouter_prober/assets/agentrouter-dashboard-token.png)

### Pair your Telegram account

Leave `TELEGRAM_CHAT_ID` blank in the project `.env` and set `OWNERSHIP_OTP`
to a private, random 10-digit code you choose. Start the bot, send `/start`
in a private chat, and reply with the code from `.env`. After the bot connects
to Telegram, you have 10 minutes to use the code. If the window expires,
`docker compose restart prober` opens a new window with the configured code.

Pairing allows 10 incorrect submissions by default, shared across all chats
that start pairing. The bot shows the remaining attempts in its prompt and
after every incorrect submission. Once the limit is reached, run
`docker compose restart prober` to reset it. To change the limit, set
`OWNERSHIP_OTP_MAX_ATTEMPTS` to a positive integer in the project `.env` and
recreate the container with `docker compose up -d --force-recreate prober`.

The bot saves `TELEGRAM_CHAT_ID` to `data/ownership/.env` using the existing
data mount. The saved owner survives container restarts and rebuilds. You can
also enter your numeric private chat ID in the project `.env` to skip pairing.
Double-check it before recreating the container. A valid ID for somebody else's
private chat assigns ownership to that person. OTP pairing avoids that risk
by verifying the Telegram account that replies. [Transfer ownership](#transfer-ownership-to-another-telegram-account)

For a private bot, keep access owner-only. The bot restricts commands and
account information to its configured or paired owner. As a second layer, use
`/setjoingroups` in `@BotFather` to disable group joins if you only use a private
chat. Telegram's group privacy setting controls group messages, and private
messages can still reach the bot. [Telegram bot settings](https://core.telegram.org/bots/features#edit-settings)

### Enter credentials in Telegram

Leave the AgentRouter values blank in the project `.env` and pair your account
before opening an AgentRouter feature.

In the registered private chat, tap **Status**, **Balance**, or any other
AgentRouter feature. The bot prompts for missing credentials one at a time,
in the order shown below. You can prefill any of the three in `.env` and enter
the rest in Telegram. Send commands and credentials as text. The bot ignores
incoming files, media, and captions, and attempts to delete credential messages
after receiving them. If deletion fails, the bot asks you to delete the message
manually and rotate the credential if it may have been exposed.

1. API key for probing models (`sk-...`)
2. Numeric AgentRouter account ID
3. Raw dashboard access token for that account

### Prefill credentials in `.env`

For Docker Compose, copy `.env.example` to `.env` in the repository and fill in
these values along with `TELEGRAM_BOT_TOKEN`. When running directly, set the
same variables in your shell. `CODEX_GATEWAY_API_KEY` is accepted as a fallback
for `AGENTROUTER_API_KEY`.

```dotenv
AGENTROUTER_API_KEY=sk-your-model-key
AGENTROUTER_USER_ID=12345
AGENTROUTER_DASHBOARD_TOKEN=your-dashboard-access-token
```

With all three values present, the bot checks them on startup, retries temporary
failures, and skips the prompts once validation passes. Use prefilled or saved
credentials when the Telegram destination is a group or channel. Interactive
setup takes place in a private chat.

### Validation and storage

The bot checks the model key with `/v1/models` and the dashboard token and
account ID together with `/api/user/self`. If AgentRouter rejects a credential,
the bot asks for the model key or the dashboard pair again. **Help** and the
main menu work during setup. Probes, monitoring, and alerts start after all
three values pass and are saved.

The bot saves the verified set to `credentials/.env` beside `STATE_PATH`, at
`/data/credentials/.env` in Docker. Saved credentials take precedence over
environment values and are checked again on startup.

To refresh the dashboard token with Compose, edit `AGENTROUTER_DASHBOARD_TOKEN`
in `data/credentials/.env`. If the bot has yet to save credentials, edit the
project `.env`. Apply the change with `docker compose up -d --force-recreate prober`.

## Start with Docker

Use Docker Compose V2 on Windows or Linux and run the commands from the
repository directory. For a new install, copy `.env.example` to `.env` and
fill in `TELEGRAM_BOT_TOKEN`. If you already have `.env.vps`, rename it to
`.env` or copy its settings into your existing `.env`.

### Prepare the data directory

Compose mounts the `data/` directory next to `compose.yaml`. Create it before
starting the service. On a Linux host using Docker Engine without user ID
remapping, prepare a new directory for the Compose service's UID/GID
`10001:10001` and restrict access to `.env`.

```sh
sudo install -d -m 700 -o 10001 -g 10001 data
chmod 600 .env
```

For an existing installation, check the service's write access before changing
directory ownership. Standalone Docker containers run as UID/GID `999:999` by
default. Run Compose with `sudo` on Linux if your Docker installation requires it.

### Start the service

Set `OWNERSHIP_OTP` or `TELEGRAM_CHAT_ID` in `.env`, then build and start the
service. [Telegram setup](#pair-your-telegram-account)

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Compose builds require either `OWNERSHIP_OTP` or `TELEGRAM_CHAT_ID` in the
project `.env`. For future builds, you can leave the OTP field set or copy the
verified chat ID from `data/ownership/.env` into the project `.env` and clear
the code. If you use an alternate `ENV_FILE`, pass the same path to
`docker compose --env-file` so Compose uses those settings for the build check.

Pair your account using the code you set in `.env`, then choose a feature to
set up AgentRouter. Use `docker compose stop` to stop the service. Shutdown
saves current state and pending alerts before closing the HTTP clients.

`docker compose run --rm prober --test-telegram` sends a test message using
the Telegram configuration alone. `docker compose run --rm prober --once`
requires all three AgentRouter credentials and runs one model cycle after
registration and validation. It exits non-zero if validation, the model status
read, or an authentication or protocol check fails.

Compose mounts `data/` at `/data` in the container. This directory preserves
`state.json`, `ownership/.env`, and `credentials/.env` across restarts and
container recreation.

## Run directly on Windows

```powershell
pip install -e ".[test]"
$env:TELEGRAM_BOT_TOKEN = "123456789:replace-me"
$env:OWNERSHIP_OTP = Read-Host "Enter a random 10-digit ownership code"
$env:STATE_PATH = "$PWD\data\state.json"
agentrouter-prober
```

Direct execution uses shell environment variables and the same setup flow.

## Telegram commands

Tap **Help** (`/help`) for a quick guide in Telegram. The bot probes AgentRouter
models automatically and sends alerts when availability changes or the
probing key is low on quota.

- **Status** (`/status`) shows the latest model probes. ✅ means available,
  ❌ means unavailable, and ❔ means unknown. Status waits for a running cycle,
  reads saved results, or starts a cycle when there are none.
- **Probe Now** (`/probe_now`) runs a background sweep of every listed model and
  sends the results when it finishes. If a sweep is running, the bot sends that
  sweep's results. A new manual sweep can start 60 seconds after the last one
  finishes. The automatic interval stays the same. If the bot restarts during
  a manual sweep, it reports that the previous result is unknown. Tap **Probe
  Now** to start a new manual sweep.
- **Balance** (`/balance`) shows your balance, total usage, request count,
  and account group. The bot uses AgentRouter’s public `/api/status` settings
  to convert quota to dollars.
- **Schedule** (`/probing_schedule`) sets automatic probes to run every
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
for Schedule.

Send `/start` to refresh the keyboard. The bot accepts commands from the
registered user in a private chat. An explicitly configured group or channel
can receive alerts. Command replies and alerts share Telegram rate limits.

## Credit alerts

Open **Credit Alerts**, choose a type, and enter a positive amount in the units
shown by **Balance**. Use up to two decimal places for dollars or a whole
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
to change the threshold. Accounts that use quota units see the equivalent
threshold in quota units. The warning fires once per low-quota period and
rearms when the quota recovers to the threshold. Unlimited keys have no
per-key balance to monitor. If the dashboard hides full key values, set
`AGENTROUTER_PROBE_KEY_ID` to the numeric ID shown beside the probing key in
**API Keys**. An exact key match takes precedence when the dashboard returns
the full key. Update the ID when you switch to a different key.

## API key monitoring

Open **API Keys** to see the shared account balance alongside the quota and
lifetime spending for each key. The bar shows the share spent, followed by
`[spent/left]`. ✅ marks active keys. ❌ marks inactive keys and appears alongside
the reason. A key’s quota is its individual spending cap on the shared account
balance. Unlimited keys show `∞`. The bot reads these fields with the dashboard
token and uses numeric key IDs to keep keys with duplicate names or renamed
keys distinct. It shows key metadata in the registered private chat.

- **Last 24 Hours** and **Last 7 Days** show spend, logged request counts, and
  the top models by spend for that key. The reports use AgentRouter’s available
  consumption logs.
- **Key Alerts** lets you set a one-time alert for additional spending, quota
  below a threshold, or a set number of days before expiry. Setting the same
  alert again replaces it. **Clear Alerts** clears the selected key’s
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
the paired recipient. Before configuring a different bot, set a fresh
`OWNERSHIP_OTP` in the project `.env` if you want to pair in Telegram. Run one
update listener per bot; a webhook or another polling instance causes Telegram
conflicts. Temporary connection failures use capped retry delays and honor
Telegram's requested rate-limit delay.

### Transfer ownership to another Telegram account

The new owner will be able to use the AgentRouter features configured on this
bot. Run these steps from the directory containing `compose.yaml`.

1. Stop the bot.

   ```sh
   docker compose stop prober
   ```

2. If `data/ownership/.env` exists, edit its `TELEGRAM_CHAT_ID` line so it reads
   `TELEGRAM_CHAT_ID=`. Leave the other lines as they are. On a Linux VPS, use
   `sudo nano data/ownership/.env` if the file requires elevated permissions.

3. In the project `.env` next to `compose.yaml`, clear `TELEGRAM_CHAT_ID` as
   well. Replace the placeholder below with a fresh, private, random 10-digit
   code. The project chat ID takes precedence over the saved chat ID when set.

   ```dotenv
   TELEGRAM_CHAT_ID=
   OWNERSHIP_OTP=REPLACE_WITH_YOUR_CODE
   ```

4. Recreate the container so Compose loads the new values.

   ```sh
   docker compose up -d --build --force-recreate prober
   ```

5. From the **new Telegram account**, open a private chat with the bot, send
   `/start`, and reply with the code you set in `.env`. The bot saves the new
   chat ID in `data/ownership/.env` and replies **Connected. Choose an action
   to get started.**

If the 10-minute pairing window expires or the attempt count reaches zero,
run `docker compose restart prober`, then send `/start` again from the new
account. The bot uses the code already set in `.env`. [Pairing limits](#pair-your-telegram-account)

If you already have the new owner's verified numeric chat ID, you can enter it
in the project `.env` in step 3 and finish after recreating the container.

With a custom `STATE_PATH`, the paired chat is saved under `ownership/.env`
beside the state file. Installations upgrading from `telegram.json` need to pair
once or set an explicit `TELEGRAM_CHAT_ID`. Unreadable or corrupt pairing files
produce an error.

## Availability behavior

- Dashboard status and metrics are saved for diagnostics. Every listed model is
  probed, including models marked `degraded`, `down`, or `no_traffic`.
- API advertisements determine the request: `responses` uses `/v1/responses`,
  `chat` uses `/v1/chat/completions`, and `claude_native` uses `/v1/messages`.
  Responses is preferred when a model advertises multiple supported APIs.
- Confirmation requires a valid API response with generated output. A
  Responses request stopped by its output-token limit also confirms
  availability when it contains output or reports generated tokens.
- HTTP 402 pool exhaustion and recognized no-channel/model-not-found responses
  mean unavailable. A 503 with a structured no-channel message also counts as
  unavailable. A generic 503 is a temporary probe error and preserves the
  previous confirmed availability. Timeouts, rate limits, and malformed replies
  are also recorded as probe errors.
- Changes in confirmed availability produce one compact list for all live
  models, grouped under **Available** and **Not available**. The list contains
  model names only; failed or unconfirmed checks appear under **Unknown**.
  Unchanged results do not repeat the notification.
- Pending lists survive shutdown. Telegram rate limits pause delivery until
  their saved retry deadline; delivery resumes on a later cycle.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTROUTER_BASE_URL` | `https://agentrouter.org` | HTTPS base URL for the status and model APIs |
| `AGENTROUTER_API_KEY` | empty | Model key for probing. Can be entered through Telegram setup. |
| `AGENTROUTER_PROBE_KEY_ID` | empty | Optional positive dashboard key ID for the probing-key quota warning |
| `TELEGRAM_CHAT_ID` | empty | Numeric chat ID or `@username` for a channel. Leave blank to pair in private. |
| `OWNERSHIP_OTP` | empty | Private, random 10-digit pairing code. Set it when pairing a new chat. |
| `OWNERSHIP_OTP_MAX_ATTEMPTS` | `10` | Total incorrect pairing submissions allowed across all chats before a restart is required. |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | HTTPS base URL for the Telegram API |
| `AGENTROUTER_DASHBOARD_TOKEN` | empty | Dashboard access token for balance, model discovery, API key metadata, and usage logs |
| `AGENTROUTER_USER_ID` | empty | Positive account ID paired with the dashboard token |
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

Docker's `--healthcheck` validates local configuration, including the presence
of `TELEGRAM_BOT_TOKEN`, and permits the bot to wait for AgentRouter setup.
Once the three AgentRouter values are present, it checks the age of the last
successful model-status read. At service startup, unreadable state data is
preserved beside `STATE_PATH` with a `.corrupt-` suffix before monitoring starts
with empty state.

Use `agentrouter-prober --telegram-status` to read the last successful Telegram
contact and its age from disk. With Compose, run
`docker compose exec prober python -m agentrouter_prober --telegram-status`.
The bot refreshes the timestamp at most once a minute. For a group or channel
destination, it advances when the bot sends a notification. This signal is
independent of Docker health.

## Troubleshooting

Run `docker compose logs --tail=80 prober` to inspect startup errors.

| Message | Action |
| --- | --- |
| `bind source path does not exist` | Create `data` next to `compose.yaml`. |
| Permission error saving state | Check that the Compose service can write to `data`. See [data directory permissions](#prepare-the-data-directory). |
| `AgentRouter rejected the dashboard token and account ID pair` | Send the matching ID and raw dashboard token in the bot's setup prompts. |
| Browser verification instead of model status | Check whether AgentRouter allows API requests from the host. |
| Telegram webhook or polling conflict | Stop other update listeners using the same Telegram token. |

## Tests

```sh
pip install -e ".[test]"
pytest tests/
```

The tests run offline with mock HTTP transports.

## Update Docker image and dependency pins

Review the pins monthly and refresh them sooner for security updates. Use
the multi-platform digest reported by
`docker buildx imagetools inspect docker.io/library/python:3.13-slim` in
`Dockerfile` so the same pin works on amd64 and arm64.

In a Python 3.13 development environment with `pip-tools` and the test
dependencies installed, refresh the hashed build and runtime requirements
together.

```sh
python -m piptools compile --upgrade --all-build-deps --allow-unsafe \
  --generate-hashes --strip-extras --no-emit-index-url \
  --pip-args='--only-binary=:all:' --output-file requirements-docker.txt pyproject.toml
python -m pip install --require-hashes --only-binary=:all: -r requirements-docker.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest tests/
docker compose build
```

Review and commit the new digest and lock file together after the offline
suite and image build pass.
