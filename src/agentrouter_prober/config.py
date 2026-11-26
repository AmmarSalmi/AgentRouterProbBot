from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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

    @classmethod
    def from_env(cls) -> "Settings":
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

        return cls(
            agentrouter_base_url=_url(
                "AGENTROUTER_BASE_URL", "https://agentrouter.org"
            ),
            agentrouter_api_key=_required_any(
                "AGENTROUTER_API_KEY", "CODEX_GATEWAY_API_KEY"
            ),
            agentrouter_dashboard_token=_required_any("AGENTROUTER_DASHBOARD_TOKEN"),
            agentrouter_user_id=_positive_int("AGENTROUTER_USER_ID"),
            telegram=TelegramSettings.from_env(),
            poll_seconds=_positive_int("POLL_SECONDS", 900, "NORMAL_POLL_SECONDS"),
            probe_max_output_tokens=_positive_int(
                "PROBE_MAX_OUTPUT_TOKENS", 32, "CONFIRM_MAX_OUTPUT_TOKENS"
            ),
            probe_concurrency=_positive_int("PROBE_CONCURRENCY", 2),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 90),
            state_path=Path(os.getenv("STATE_PATH", "/data/state.json")),
            health_max_age_seconds=_positive_int("HEALTH_MAX_AGE_SECONDS", 3600),
            client_user_agent=os.getenv(
                "AGENTROUTER_CLIENT_USER_AGENT",
                "codex_cli_rs/0.171.0 (Windows 11; x86_64)",
            ).strip(),
            reasoning_effort=reasoning_effort,
            probe_key_low_balance_usd=key_alert_threshold,
        )
