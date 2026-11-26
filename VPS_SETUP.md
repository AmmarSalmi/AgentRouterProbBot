# Run the bot on a Linux VPS

The `invalid volume specification: C:/Users/...` error occurs because a Linux
Docker host cannot mount a Windows drive path. On the VPS, use
`compose.vps.yaml`. It mounts folders next to the Compose file and uses the
existing `Dockerfile`; no Dockerfile changes are needed.

| Folder on the VPS | Path inside the container | Purpose |
| --- | --- | --- |
| `/opt/AgentRouterProbBot/zen-profile` | `/zen-profile` (read-only) | Optional exported AgentRouter session |
| `/opt/AgentRouterProbBot/data` | `/data` | Saved results and Telegram registration |
| `/opt/AgentRouterProbBot/codex` | `/codex` | Bot-managed Codex config |

The VPS needs Docker Engine and Docker Compose V2. To use a dashboard token,
deploy the code to `/opt/AgentRouterProbBot` and go to step 3. Supply the raw
token and its numeric account ID in `.env.vps`. Docker can create an empty
`zen-profile` folder. [Set Linux permissions and credentials](#3-set-linux-permissions-and-credentials)

For Zen authentication, follow steps 1 and 2 to export the AgentRouter cookie
and matching user ID in two standalone SQLite databases. The export preserves
the Firefox container folder name, including suffixes such as `^userContextId=6`.

The archive contains a login credential. Keep it private. The repository and
Docker ignore rules exclude exports, deployed profiles, live environment files,
and runtime data.

The prepared VPS code archive also uses the VPS configuration as its default
`compose.yaml`. The commands below explicitly select `compose.vps.yaml`, so
they work with either the archive or a Git checkout containing these updates.

## 1. Export on Windows 10 for Zen authentication

Use Python 3.11 or newer. The export script uses only the standard library and
can run from a checkout without installing the bot's dependencies.

Log into AgentRouter in your Zen profile, then run from the project folder:

```powershell
py scripts/export_zen_session.py
```

This discovers the default Zen profile using `%APPDATA%\zen\profiles.ini` and
creates `exports\zen-profile.tar.gz`. If you use a different profile, specify
its root directory:

```powershell
py scripts/export_zen_session.py --profile "C:\Users\YourName\AppData\Roaming\zen\Profiles\YOUR_PROFILE"
```

The exporter reads current WAL data while Zen is open and writes fresh SQLite
images. It excludes cookies from other sites and strips the local-storage user
record down to its ID. If Zen's databases are locked, close Zen and rerun the
export. Keep the paths inside the archive unchanged.

Before starting the VPS bot, stop the Windows bot so only one instance polls
Telegram:

```powershell
docker compose stop prober
```

## 2. Upload the code and session

For this prepared deployment, `exports\AgentRouterProbBot-vps.tar.gz` contains
the current code, VPS Compose file, and templates. It excludes credentials and
the session export. Transfer both archives separately to your VPS home folder:

```powershell
scp .\exports\AgentRouterProbBot-vps.tar.gz .\exports\zen-profile.tar.gz VPS_USER@VPS_IP:~/
ssh VPS_USER@VPS_IP
```

Replace `VPS_USER` and `VPS_IP` with your SSH login. WinSCP/SFTP can also upload
the two archives to the same home folder.

The following commands assume your VPS login can use `sudo`. If you log in as
root, omit `sudo`.

```sh
chmod 600 ~/zen-profile.tar.gz
sudo mkdir -p /opt/AgentRouterProbBot
sudo tar -xzf ~/AgentRouterProbBot-vps.tar.gz -C /opt
cd /opt/AgentRouterProbBot
sudo tar -xzf ~/zen-profile.tar.gz -C /opt/AgentRouterProbBot
```

The resulting profile layout is:

```text
zen-profile/
  cookies.sqlite
  storage/
    default/
      https+++agentrouter.org[original container suffix]/
        ls/
          data.sqlite
```

The actual origin folder is already in the archive. For example, it can be
`https+++agentrouter.org^userContextId=6`; keep that name as exported.

## 3. Set Linux permissions and credentials

The VPS Compose file runs the bot as UID/GID `10001:10001`. Give that user
access to its data and config folders. The config sample enables the model
picker without copying your entire Windows Codex directory.

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

Fill in your actual values:

```dotenv
AGENTROUTER_API_KEY=your-agentrouter-api-key
AGENTROUTER_DASHBOARD_TOKEN=your-dashboard-access-token
AGENTROUTER_USER_ID=your-numeric-account-id
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=
AGENTROUTER_BASE_URL=https://agentrouter.org
POLL_SECONDS=900
PROBE_MAX_OUTPUT_TOKENS=32
PROBE_REASONING_EFFORT=low
PROBE_CONCURRENCY=2
REQUEST_TIMEOUT_SECONDS=90
HEALTH_MAX_AGE_SECONDS=3600
LOG_LEVEL=INFO
```

Leave `TELEGRAM_CHAT_ID` blank to register your private chat through `/start`.
If you use the profile export, leave `AGENTROUTER_DASHBOARD_TOKEN`,
`AGENTROUTER_SESSION_COOKIE`, and `AGENTROUTER_USER_ID` blank and set the
exported files' permissions:

```sh
sudo chown -R 10001:10001 zen-profile
sudo find zen-profile -type d -exec chmod 700 {} \;
sudo find zen-profile -type f -exec chmod 600 {} \;
```

The VPS model picker edits `/opt/AgentRouterProbBot/codex/config.toml`. It does
not edit the Codex config on your Windows computer. Mount the actual Codex
directory on the VPS if you want the picker to control that installation.

## 4. Validate and start

Use the VPS Compose file for each command:

```sh
cd /opt/AgentRouterProbBot
sudo docker compose --env-file .env.vps -f compose.vps.yaml config --quiet
sudo docker compose --env-file .env.vps -f compose.vps.yaml build
```

For a Zen export, run the local check below. It reads the mounted session
files and checks cookie expiry without making network requests.

```sh
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --check-session
```

Test Telegram, then one live model cycle:

```sh
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --test-telegram
```

When it waits for `/start`, open your bot's private chat and send `/start`.
Wait for the confirmation and test message. Then run:

```sh
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --once
sudo docker compose --env-file .env.vps -f compose.vps.yaml up -d
sudo docker compose --env-file .env.vps -f compose.vps.yaml logs --tail=80 prober
sudo docker compose --env-file .env.vps -f compose.vps.yaml ps
```

Check Status, Balance & Usage, and Probe Now in Telegram. Availability
notifications contain simple lists, while Status contains model names with ✅ or
❌. Detailed results remain in `data/state.json`, and registration remains in
`data/telegram.json`.

## 5. Refresh an expired credential

For dashboard authentication, update `AGENTROUTER_DASHBOARD_TOKEN` in
`.env.vps` and recreate the service with
`sudo docker compose --env-file .env.vps -f compose.vps.yaml up -d prober`.

On Windows, log into AgentRouter again in the same Zen profile. Export again:

```powershell
py scripts/export_zen_session.py
scp .\exports\zen-profile.tar.gz VPS_USER@VPS_IP:~/
```

For a non-default profile, repeat your earlier `--profile` argument. Upload
only the new session archive; the code archive is not needed for a session
refresh.

On the VPS:

```sh
chmod 600 ~/zen-profile.tar.gz
cd /opt/AgentRouterProbBot
sudo docker compose --env-file .env.vps -f compose.vps.yaml stop prober
sudo tar -xzf ~/zen-profile.tar.gz -C /opt/AgentRouterProbBot
sudo chown -R 10001:10001 zen-profile
sudo find zen-profile -type d -exec chmod 700 {} \;
sudo find zen-profile -type f -exec chmod 600 {} \;
sudo docker compose --env-file .env.vps -f compose.vps.yaml run --rm prober --check-session
sudo docker compose --env-file .env.vps -f compose.vps.yaml start prober
sudo docker compose --env-file .env.vps -f compose.vps.yaml logs --tail=80 prober
```

This replaces the session files and restarts the reader while preserving saved
results, notification history, Telegram registration, and Codex config. An
image rebuild is not needed for session refreshes.

## Troubleshooting

| Message | Action |
| --- | --- |
| `invalid volume specification: C:/...` | Select `-f compose.vps.yaml`. Its sources are Linux folders; do not use the Windows host paths on the VPS. |
| `bind source path does not exist` | Create `data` and `codex` next to `compose.vps.yaml`. |
| `AgentRouter rejected dashboard authentication` | Check that `.env.vps` contains the raw dashboard access token and the numeric account ID for the same account. |
| `Zen cookie database could not be read` | Check the folder and file ownership for UID/GID 10001 and the exported database files. |
| `No logged-in AgentRouter session...` | Log into the correct Zen profile, re-export, upload, and restart. Preserve the exported storage folder names. |
| `AgentRouter rejected the Zen login session` | Re-export after logging in again. If a fresh export still fails only on the VPS, check whether AgentRouter permits that VPS IP/session. |
| Browser verification instead of model status | The files are mounted, but AgentRouter is challenging access from the VPS. A copied session does not guarantee acceptance from a different IP. |
| Telegram webhook/another-instance conflict | Stop the Windows bot and other update listeners using this token before starting the VPS listener. |
| Permission error saving state/config | Check that `data` and `codex` are writable by UID/GID 10001. |

The session export keeps an existing login; it does not extend its lifetime.
AgentRouter can revoke it before its stored cookie expiry. The live `--once`
test is the check that confirms authentication and model requests from your VPS.
