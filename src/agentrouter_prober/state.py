from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import Any, Literal
from uuid import uuid4


UTC = timezone.utc
LOGGER = logging.getLogger(__name__)
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


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                LOGGER.warning("Could not sync the state directory at %s", path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class TelegramContactStore:
    def __init__(self, state_path: Path, api_base_url: str, bot_token: str):
        self.path = state_path.with_name(f"{state_path.name}.telegram-contact.json")
        self.scope = sha256(f"{api_base_url}\0{bot_token}".encode()).hexdigest()
        self._next_write_at = float("-inf")

    def record(self) -> None:
        now = monotonic()
        if now < self._next_write_at:
            return
        self._next_write_at = now + 60
        try:
            write_json_atomic(self.path, {"scope": self.scope, "contact_at": format_datetime(utc_now())})
        except OSError as exc:
            LOGGER.warning("Could not save the Telegram contact time: %s", exc)

    def load(self) -> datetime | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("scope") != self.scope:
            return None
        return parse_datetime(raw.get("contact_at"))


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


@dataclass(frozen=True)
class ManualProbeState:
    scope: str
    last_update_id: int
    last_requested_at: str
    pending_update_id: int | None = None
    last_completed_at: str | None = None
    interruption_reported: bool = False

    @classmethod
    def from_dict(cls, raw: object) -> ManualProbeState | None:
        if not isinstance(raw, dict):
            return None
        scope = raw.get("scope")
        last_update_id = raw.get("last_update_id")
        last_requested_at = raw.get("last_requested_at")
        pending = raw.get("pending_update_id")
        completed = raw.get("last_completed_at")
        if (
            not isinstance(scope, str) or not scope
            or type(last_update_id) is not int or last_update_id < 0
            or not isinstance(last_requested_at, str) or parse_datetime(last_requested_at) is None
            or (pending is not None and (type(pending) is not int or not 0 <= pending <= last_update_id))
        ):
            return None
        return cls(
            scope, last_update_id, last_requested_at, pending,
            completed if isinstance(completed, str) else None,
            raw.get("interruption_reported") is True,
        )


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
    manual_probe: ManualProbeState | None = None

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
        stored_models = raw.get("models", {})
        if not isinstance(stored_models, dict):
            raise ValueError("state models are not an object")
        models = {
            name: ModelRecord.from_dict(record)
            for name, record in stored_models.items()
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
            manual_probe=ManualProbeState.from_dict(raw.get("manual_probe")),
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
            "manual_probe": asdict(self.manual_probe) if self.manual_probe else None,
        }


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, *, recover: bool = True) -> PersistentState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            return PersistentState.from_dict(raw)
        except FileNotFoundError:
            return PersistentState()
        except (ValueError, TypeError):
            if recover:
                backup = self.path.with_name(f"{self.path.name}.corrupt-{uuid4().hex}")
                self.path.rename(backup)
                LOGGER.warning("Preserved unreadable state at %s and started with empty state", backup)
            return PersistentState(last_error="Existing state file was unreadable")

    def save(self, state: PersistentState) -> None:
        write_json_atomic(self.path, state.to_dict())


def health_is_fresh(path: Path, max_age_seconds: int, now: datetime | None = None) -> bool:
    state = StateStore(path).load(recover=False)
    successful_at = parse_datetime(state.last_successful_status_at)
    if successful_at is None:
        return False
    current = now or utc_now()
    age = (current.astimezone(UTC) - successful_at).total_seconds()
    return 0 <= age <= max_age_seconds
