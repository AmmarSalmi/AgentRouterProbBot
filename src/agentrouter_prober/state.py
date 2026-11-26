from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


UTC = timezone.utc
CreditAlertKind = Literal["spending", "balance"]
CREDIT_ALERT_KINDS: tuple[CreditAlertKind, ...] = ("spending", "balance")
KeyAlertKind = Literal["spending", "balance", "expiry"]
KEY_ALERT_KINDS: tuple[KeyAlertKind, ...] = (*CREDIT_ALERT_KINDS, "expiry")


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass
class ModelRecord:
    availability: str = "unknown"
    dashboard_status: str | None = None
    dashboard_tier: str | None = None
    apis: list[str] = field(default_factory=list)
    success_rate_24h: float | None = None
    ttft_p90_ms_24h: int | None = None
    avg_latency_ms: int | None = None
    last_observed_at: str | None = None
    last_probed_at: str | None = None
    last_confirmed_at: str | None = None
    last_notified_at: str | None = None
    probe_api: str | None = None
    probe_latency_ms: int | None = None
    probe_reply: str | None = None
    probe_error: str | None = None
    notification_pending: bool = False
    # Kept for migration from version 1 state; no longer used for scheduling.
    confirm_failures: int = 0
    next_confirm_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelRecord":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: raw[key] for key in allowed if key in raw})


@dataclass(frozen=True)
class CreditAlert:
    threshold_quota: int
    chat_id: str
    account_id: int
    pending_message: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> CreditAlert | None:
        if not isinstance(raw, dict):
            return None
        threshold = raw.get("threshold_quota")
        chat_id = raw.get("chat_id")
        account_id = raw.get("account_id")
        message = raw.get("pending_message")
        if (
            type(threshold) is not int
            or threshold <= 0
            or not isinstance(chat_id, str)
            or not chat_id
            or type(account_id) is not int
            or account_id <= 0
        ):
            return None
        return cls(
            threshold, chat_id, account_id, message if isinstance(message, str) and message else None
        )


@dataclass(frozen=True)
class ProbeKeyAlert:
    key_fingerprint: str
    chat_id: str
    pending_message: str | None

    @classmethod
    def from_dict(cls, raw: object) -> ProbeKeyAlert | None:
        if not isinstance(raw, dict):
            return None
        fingerprint = raw.get("key_fingerprint")
        chat_id = raw.get("chat_id")
        message = raw.get("pending_message")
        if not isinstance(fingerprint, str) or not isinstance(chat_id, str):
            return None
        return cls(fingerprint, chat_id, message if isinstance(message, str) else None)


@dataclass(frozen=True)
class KeyAlert:
    key_id: int
    kind: KeyAlertKind
    threshold: int  # Quota units for spending and balance, days for expiry.
    pending_message: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> KeyAlert | None:
        if not isinstance(raw, dict):
            return None
        key_id, kind, threshold = raw.get("key_id"), raw.get("kind"), raw.get("threshold")
        if (
            type(key_id) is not int or key_id <= 0 or kind not in KEY_ALERT_KINDS
            or type(threshold) is not int or threshold <= 0
        ):
            return None
        message = raw.get("pending_message")
        return cls(key_id, kind, threshold, message if isinstance(message, str) else None)


@dataclass(frozen=True)
class WeeklyDigest:
    week_end: int
    pending_message: str | None = None


@dataclass(frozen=True)
class KeyMonitoring:
    scope: str
    key_alerts: dict[tuple[int, KeyAlertKind], KeyAlert] = field(default_factory=dict)
    weekly_digest: WeeklyDigest | None = None

    @classmethod
    def from_dict(cls, raw: object) -> KeyMonitoring | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("scope"), str):
            return None
        alerts = {}
        if isinstance(raw.get("key_alerts"), list):
            for item in raw["key_alerts"]:
                if alert := KeyAlert.from_dict(item):
                    alerts[alert.key_id, alert.kind] = alert
        digest = None
        saved = raw.get("weekly_digest")
        if isinstance(saved, dict) and type(saved.get("week_end")) is int and saved["week_end"] > 0:
            message = saved.get("pending_message")
            digest = WeeklyDigest(saved["week_end"], message if isinstance(message, str) else None)
        return cls(raw["scope"], alerts, digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key_alerts": [asdict(alert) for alert in self.key_alerts.values()],
            "weekly_digest": asdict(self.weekly_digest) if self.weekly_digest else None,
        }


@dataclass
class PersistentState:
    version: int = 2
    models: dict[str, ModelRecord] = field(default_factory=dict)
    active_models: list[str] = field(default_factory=list)
    last_cycle_at: str | None = None
    last_successful_status_at: str | None = None
    last_error: str | None = None
    telegram_retry_at: str | None = None
    poll_seconds_override: int | None = None
    credit_alerts: dict[CreditAlertKind, CreditAlert] = field(default_factory=dict)
    probe_key_alert: ProbeKeyAlert | None = None
    key_monitoring: KeyMonitoring | None = None

    def model(self, name: str) -> ModelRecord:
        return self.models.setdefault(name, ModelRecord())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PersistentState":
        poll_seconds_override = raw.get("poll_seconds_override")
        credit_alerts: dict[CreditAlertKind, CreditAlert] = {}
        stored_alerts = raw.get("credit_alerts")
        if isinstance(stored_alerts, dict):
            for kind in CREDIT_ALERT_KINDS:
                alert = CreditAlert.from_dict(stored_alerts.get(kind))
                if alert is not None:
                    credit_alerts[kind] = alert
        models = {
            name: ModelRecord.from_dict(record)
            for name, record in raw.get("models", {}).items()
            if isinstance(record, dict)
        }
        return cls(
            version=2,
            models=models,
            active_models=[
                name for name in raw.get("active_models", []) if isinstance(name, str)
            ],
            last_cycle_at=raw.get("last_cycle_at"),
            last_successful_status_at=raw.get("last_successful_status_at"),
            last_error=raw.get("last_error"),
            telegram_retry_at=raw.get("telegram_retry_at"),
            poll_seconds_override=(
                poll_seconds_override
                if type(poll_seconds_override) is int and poll_seconds_override > 0
                else None
            ),
            credit_alerts=credit_alerts,
            probe_key_alert=ProbeKeyAlert.from_dict(raw.get("probe_key_alert")),
            key_monitoring=KeyMonitoring.from_dict(raw.get("key_monitoring")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_models": self.active_models,
            "models": {
                name: asdict(record) for name, record in sorted(self.models.items())
            },
            "last_cycle_at": self.last_cycle_at,
            "last_successful_status_at": self.last_successful_status_at,
            "last_error": self.last_error,
            "telegram_retry_at": self.telegram_retry_at,
            "poll_seconds_override": self.poll_seconds_override,
            "credit_alerts": {
                kind: asdict(alert) for kind, alert in self.credit_alerts.items()
            },
            "probe_key_alert": asdict(self.probe_key_alert) if self.probe_key_alert else None,
            "key_monitoring": self.key_monitoring.to_dict() if self.key_monitoring else None,
        }


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> PersistentState:
        if not self.path.exists():
            return PersistentState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            return PersistentState.from_dict(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return PersistentState(last_error="Existing state file was unreadable")

    def save(self, state: PersistentState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def health_is_fresh(path: Path, max_age_seconds: int, now: datetime | None = None) -> bool:
    state = StateStore(path).load()
    successful_at = parse_datetime(state.last_successful_status_at)
    if successful_at is None:
        return False
    current = now or utc_now()
    age = (current.astimezone(UTC) - successful_at).total_seconds()
    return 0 <= age <= max_age_seconds
