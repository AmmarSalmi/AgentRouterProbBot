from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .state import write_text_atomic


MODEL_KEY = "AGENTROUTER_API_KEY"
ACCOUNT_ID = "AGENTROUTER_USER_ID"
DASHBOARD_TOKEN = "AGENTROUTER_DASHBOARD_TOKEN"
AGENTROUTER_CREDENTIALS = (MODEL_KEY, ACCOUNT_ID, DASHBOARD_TOKEN)
DEFAULT_OWNERSHIP_OTP_MAX_ATTEMPTS = 10


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _required_any(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise ConfigurationError(f"{' or '.join(names)} is required")


def _positive_int(name: str, default: int | None = None, *fallback_names: str) -> int:
    raw = ""
    for candidate in (name, *fallback_names):
        raw = os.getenv(candidate, "").strip()
        if raw:
            break
    if not raw:
        if default is None:
            raise ConfigurationError(f"{name} is required")
        raw = str(default)
    return _parse_positive_int(name, raw)


def _parse_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def parse_agentrouter_credential(name: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    if name == ACCOUNT_ID:
        return str(_parse_positive_int(name, value))
    if not value.isascii() or not value.isprintable() or any(char.isspace() for char in value):
        raise ConfigurationError(f"{name} must be a single token without spaces")
    return value


def _agentrouter_env_path(state_path: Path) -> Path:
    return state_path.parent / "credentials" / ".env"


def load_agentrouter_credentials(state_path: Path, *, required: bool = False) -> dict[str, str]:
    path = _agentrouter_env_path(state_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError):
        raise ConfigurationError(f"Cannot read saved AgentRouter credentials from {path}") from None

    values = {name: os.getenv(name, "") for name in AGENTROUTER_CREDENTIALS}
    if not values[MODEL_KEY].strip():
        values[MODEL_KEY] = os.getenv("CODEX_GATEWAY_API_KEY", "")
    for line in lines:
        name, _, value = line.partition("=")
        if name not in values or not value:
            continue
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, str):
            values[name] = value

    credentials = {}
    for name, value in values.items():
        try:
            credentials[name] = parse_agentrouter_credential(name, value)
        except ConfigurationError:
            if required:
                raise
    return credentials


def save_agentrouter_credentials(state_path: Path, credentials: dict[str, str]) -> None:
    contents = "".join(f"{name}={json.dumps(credentials[name])}\n" for name in AGENTROUTER_CREDENTIALS)
    write_text_atomic(_agentrouter_env_path(state_path), contents)


def _https_url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    error = f"{name} must be an absolute HTTPS URL without user information, a query, or a fragment"
    try:
        parsed = httpx.URL(value)
    except httpx.InvalidURL:
        raise ConfigurationError(error) from None
    authority = value.partition("://")[2].partition("/")[0]
    if (
        parsed.scheme != "https" or not parsed.is_absolute_url
        or "@" in authority or "?" in value or "#" in value
    ):
        raise ConfigurationError(error)
    return value


def state_path_from_env() -> Path:
    path = Path(os.getenv("STATE_PATH", "/data/state.json"))
    if not path.name:
        raise ConfigurationError("STATE_PATH must name a file")
    return path


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = field(repr=False)
    chat_id: str | None = None
    api_base_url: str = "https://api.telegram.org"
    ownership_otp: str | None = field(default=None, repr=False)
    ownership_otp_max_attempts: int = DEFAULT_OWNERSHIP_OTP_MAX_ATTEMPTS

    @classmethod
    def from_env(cls) -> "TelegramSettings":
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        if chat_id is not None and not re.fullmatch(
            r"[+-]?[0-9]*[1-9][0-9]*|@[A-Za-z0-9_]+", chat_id
        ):
            raise ConfigurationError("TELEGRAM_CHAT_ID must be a nonzero chat ID or an @username")
        return cls(
            bot_token=_required_any("TELEGRAM_BOT_TOKEN"),
            chat_id=chat_id,
            api_base_url=_https_url("TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
            ownership_otp=os.getenv("OWNERSHIP_OTP", "").strip() or None,
            ownership_otp_max_attempts=_positive_int(
                "OWNERSHIP_OTP_MAX_ATTEMPTS", DEFAULT_OWNERSHIP_OTP_MAX_ATTEMPTS
            ),
        )


@dataclass(frozen=True)
class Settings:
    agentrouter_base_url: str
    agentrouter_api_key: str = field(repr=False)
    agentrouter_dashboard_token: str = field(repr=False)
    agentrouter_user_id: int
    telegram: TelegramSettings
    poll_seconds: int
    probe_max_output_tokens: int
    probe_concurrency: int
    request_timeout_seconds: int
    state_path: Path
    health_max_age_seconds: int
    client_user_agent: str
    reasoning_effort: str = "low"
    probe_key_low_balance_usd: Decimal = Decimal("1.00")
    agentrouter_probe_key_id: int | None = None

    @classmethod
    def from_env(cls, *, require_agentrouter: bool = False) -> "Settings":
        reasoning_effort = os.getenv("PROBE_REASONING_EFFORT", "low").strip().lower()
        if reasoning_effort not in {"minimal", "low", "medium", "high"}:
            raise ConfigurationError(
                "PROBE_REASONING_EFFORT must be minimal, low, medium, or high"
            )
        raw_threshold = os.getenv("PROBE_KEY_LOW_BALANCE_USD", "").strip() or "1.00"
        threshold_error = "PROBE_KEY_LOW_BALANCE_USD must be a positive USD amount"
        try:
            key_alert_threshold = Decimal(raw_threshold)
        except InvalidOperation as exc:
            raise ConfigurationError(threshold_error) from exc
        if not key_alert_threshold.is_finite() or key_alert_threshold <= 0:
            raise ConfigurationError(threshold_error)
        probe_key_id = os.getenv("AGENTROUTER_PROBE_KEY_ID", "").strip()

        state_path = state_path_from_env()
        credentials = load_agentrouter_credentials(state_path, required=require_agentrouter)
        return cls(
            agentrouter_base_url=_https_url(
                "AGENTROUTER_BASE_URL", "https://agentrouter.org"
            ),
            agentrouter_api_key=credentials.get(MODEL_KEY, ""),
            agentrouter_dashboard_token=credentials.get(DASHBOARD_TOKEN, ""),
            agentrouter_user_id=int(credentials.get(ACCOUNT_ID, "0")),
            telegram=TelegramSettings.from_env(),
            poll_seconds=_positive_int("POLL_SECONDS", 900, "NORMAL_POLL_SECONDS"),
            probe_max_output_tokens=_positive_int(
                "PROBE_MAX_OUTPUT_TOKENS", 32, "CONFIRM_MAX_OUTPUT_TOKENS"
            ),
            probe_concurrency=_positive_int("PROBE_CONCURRENCY", 2),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 90),
            state_path=state_path,
            health_max_age_seconds=_positive_int("HEALTH_MAX_AGE_SECONDS", 3600),
            client_user_agent=os.getenv(
                "AGENTROUTER_CLIENT_USER_AGENT",
                "codex_cli_rs/0.171.0 (Windows 11; x86_64)",
            ).strip(),
            reasoning_effort=reasoning_effort,
            probe_key_low_balance_usd=key_alert_threshold,
            agentrouter_probe_key_id=(
                _parse_positive_int("AGENTROUTER_PROBE_KEY_ID", probe_key_id) if probe_key_id else None
            ),
        )
