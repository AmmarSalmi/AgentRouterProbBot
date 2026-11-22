from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _required_any(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise ConfigurationError(f"{' or '.join(names)} is required")


def _positive_int(name: str, default: int, *fallback_names: str) -> int:
    raw = ""
    for candidate in (name, *fallback_names):
        raw = os.getenv(candidate, "").strip()
        if raw:
            break
    if not raw:
        raw = str(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an HTTP(S) URL")
    return value


def _discover_zen_profile() -> Path | None:
    explicit = os.getenv("ZEN_PROFILE_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    appdata = os.getenv("APPDATA", "").strip()
    if not appdata:
        return None
    root = Path(appdata) / "zen"
    ini_path = root / "profiles.ini"
    if not ini_path.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None

    candidates: list[tuple[int, Path]] = []
    for section in parser.sections():
        if not (section.startswith("Profile") or section.startswith("Install")):
            continue
        raw_path = parser.get(section, "Path", fallback="") or parser.get(
            section, "Default", fallback=""
        )
        if not raw_path:
            continue
        path = Path(raw_path)
        if parser.get(section, "IsRelative", fallback="1") != "0":
            path = root / path
        priority = 2 if parser.get(section, "Default", fallback="0") == "1" else 1
        if section.startswith("Install"):
            priority = 3
        candidates.append((priority, path))
    for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        if (candidate / "cookies.sqlite").is_file():
            return candidate
    return None


def _codex_config_path() -> Path:
    explicit = os.getenv("CODEX_CONFIG_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    chat_id: str | None = None
    api_base_url: str = "https://api.telegram.org"

    @classmethod
    def from_env(cls) -> "TelegramSettings":
        return cls(
            bot_token=_required_any("TELEGRAM_BOT_TOKEN"),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
            api_base_url=_url("TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
        )


@dataclass(frozen=True)
class Settings:
    agentrouter_base_url: str
    agentrouter_api_key: str
    telegram: TelegramSettings
    poll_seconds: int
    probe_max_output_tokens: int
    probe_concurrency: int
    request_timeout_seconds: int
    state_path: Path
    health_max_age_seconds: int
    zen_profile_path: Path | None
    agentrouter_session_cookie: str | None
    agentrouter_user_id: int | None
    codex_config_path: Path
    client_user_agent: str
    reasoning_effort: str = "low"

    @classmethod
    def from_env(cls) -> "Settings":
        reasoning_effort = os.getenv("PROBE_REASONING_EFFORT", "low").strip().lower()
        if reasoning_effort not in {"minimal", "low", "medium", "high"}:
            raise ConfigurationError(
                "PROBE_REASONING_EFFORT must be minimal, low, medium, or high"
            )

        session_cookie = os.getenv("AGENTROUTER_SESSION_COOKIE", "").strip() or None
        user_id = _optional_positive_int("AGENTROUTER_USER_ID")
        zen_profile = _discover_zen_profile()
        if bool(session_cookie) != bool(user_id):
            raise ConfigurationError(
                "AGENTROUTER_SESSION_COOKIE and AGENTROUTER_USER_ID must be set together"
            )
        if session_cookie is None and zen_profile is None:
            raise ConfigurationError(
                "ZEN_PROFILE_PATH is required unless an AgentRouter session cookie and user ID are provided"
            )

        return cls(
            agentrouter_base_url=_url(
                "AGENTROUTER_BASE_URL", "https://agentrouter.org"
            ),
            agentrouter_api_key=_required_any(
                "AGENTROUTER_API_KEY", "CODEX_GATEWAY_API_KEY"
            ),
            telegram=TelegramSettings.from_env(),
            poll_seconds=_positive_int("POLL_SECONDS", 900, "NORMAL_POLL_SECONDS"),
            probe_max_output_tokens=_positive_int(
                "PROBE_MAX_OUTPUT_TOKENS", 32, "CONFIRM_MAX_OUTPUT_TOKENS"
            ),
            probe_concurrency=_positive_int("PROBE_CONCURRENCY", 2),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 90),
            state_path=Path(os.getenv("STATE_PATH", "/data/state.json")),
            health_max_age_seconds=_positive_int("HEALTH_MAX_AGE_SECONDS", 3600),
            zen_profile_path=zen_profile,
            agentrouter_session_cookie=session_cookie,
            agentrouter_user_id=user_id,
            codex_config_path=_codex_config_path(),
            client_user_agent=os.getenv(
                "AGENTROUTER_CLIENT_USER_AGENT",
                "codex_cli_rs/0.171.0 (Windows 11; x86_64)",
            ).strip(),
            reasoning_effort=reasoning_effort,
        )
