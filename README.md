# AgentRouter Availability Prober

A Telegram bot that imports your AgentRouter login from Zen Browser, discovers
the live model list from `/api/user/model-status`, and sends one small greeting
probe to every returned model through its advertised API. It reports model
availability and provides a picker for Codex's default model.

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
3. A Zen profile logged into AgentRouter. Open the model-status page in that
   profile before starting the service.

The bot reads the profile's `cookies.sqlite` and matching local-storage database
to obtain the session cookie and user ID. A dashboard/system access token is
not required. To use an explicit session instead, set both
`AGENTROUTER_SESSION_COOKIE` and `AGENTROUTER_USER_ID`.

Leave `TELEGRAM_CHAT_ID` blank or omit it. The first person who sends `/start`
in the bot's private chat becomes its saved recipient. An explicitly configured
chat ID skips registration.

The Codex picker requires an existing `config.toml` with a
`[model_providers.AgentRouter]` provider. The config directory must be writable
so the bot can create its backup and replace the file.

## Start with Docker

```sh
cp .env.example .env
# Edit .env with your API key, bot token, Zen profile path, and Codex directory.
docker compose build
docker compose run --rm prober --test-telegram
# When it says "Waiting for /start", open the bot and press Start.
docker compose run --rm prober --once
docker compose up -d
docker compose logs -f prober
```

`ZEN_PROFILE_SOURCE` is the host's Zen profile directory. `CODEX_HOME_SOURCE`
is the host directory containing Codex's `config.toml`. Compose mounts the Zen
profile read-only at `/zen-profile` and the Codex directory at `/codex`, and
sets the corresponding paths inside the container.

For configuration validation using placeholder credentials, in PowerShell:

```powershell
$env:ENV_FILE = ".env.example"
docker compose --env-file .env.example config --quiet
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

The named volume preserves `/data/state.json` and `/data/telegram.json` across
restarts. `docker compose down` preserves it; `docker compose down -v` removes
saved results and registration.

## Run directly on Windows

```powershell
pip install -e ".[test]"
$env:AGENTROUTER_API_KEY = "sk-replace-me"
$env:TELEGRAM_BOT_TOKEN = "123456789:replace-me"
$env:ZEN_PROFILE_PATH = "C:\Users\YourName\AppData\Roaming\zen\Profiles\xxxxxxxx.Default (release)"
$env:CODEX_CONFIG_PATH = "D:\ProgramFiles\Codex\config.toml"
$env:STATE_PATH = "$PWD\data\state.json"
agentrouter-prober
```

Direct execution reads environment variables; it does not load `.env`
automatically. Without `ZEN_PROFILE_PATH`, Windows profile discovery uses
`%APPDATA%\zen\profiles.ini`. Without `CODEX_CONFIG_PATH`, the config path is
`CODEX_HOME/config.toml` when `CODEX_HOME` is set, otherwise
`~/.codex/config.toml`.

## Telegram menu and Codex model selection

The running bot offers four keyboard buttons and matching menu commands:

- **Status** (`/status`) shows each model name with ✅ for confirmed availability
  or ❌ otherwise. Detailed metrics, APIs, latency, timestamps, and errors remain
  tracked in saved state. If no results exist, it starts a cycle; if a cycle is
  running, it waits for that cycle. Reading existing results adds no probes.
- **Probe Now** (`/probe_now`, also `/probe`) refreshes model-status and probes
  every returned model immediately.
- **Set Codex Model** (`/set_model`, also `/models`) offers only live models
  advertising `responses`. Selecting one sets the top-level `model` and
  `model_provider = "AgentRouter"` in the configured Codex file and creates
  `config.toml.agentrouter-prober.bak` before replacing it. Restart an existing
  Codex session to use the new default.
- **Probing Schedule** (`/probing_schedule`, also `/schedule`) shows the cycle
  interval and probe concurrency. Viewing it adds no model requests.

Send `/start` again to show the keyboard. Commands and model-picker callbacks
are accepted only from the registered user in that private chat. Other users,
groups, and bots cannot run probes or change the Codex config. An explicitly
configured group/channel can receive alerts, but commands require a private
chat. Command replies and alerts share Telegram rate limits.

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
| `ZEN_PROFILE_SOURCE` | required by Compose | Host Zen profile directory |
| `CODEX_HOME_SOURCE` | required by Compose | Host directory containing Codex config |
| `ZEN_PROFILE_PATH` | Windows discovery | Zen profile; Compose sets `/zen-profile` |
| `CODEX_CONFIG_PATH` | Codex home config | Codex TOML; Compose sets `/codex/config.toml` |
| `AGENTROUTER_SESSION_COOKIE` | empty | Explicit session, paired with user ID |
| `AGENTROUTER_USER_ID` | empty | Positive user ID, paired with session cookie |
| `POLL_SECONDS` | `900` | Interval between full probe cycles |
| `PROBE_MAX_OUTPUT_TOKENS` | `32` | Output-token limit for each greeting |
| `PROBE_REASONING_EFFORT` | `low` | `minimal`, `low`, `medium`, or `high` for Responses |
| `PROBE_CONCURRENCY` | `2` | Maximum simultaneous model probes |
| `REQUEST_TIMEOUT_SECONDS` | `90` | HTTP timeout |
| `STATE_PATH` | `/data/state.json` | Saved results; registration uses the same directory |
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

The tests use mock HTTP transports and synthetic Zen/Codex files. They do not
send Telegram messages or make live model requests.
