from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc


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

    def model(self, name: str) -> ModelRecord:
        return self.models.setdefault(name, ModelRecord())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PersistentState":
        poll_seconds_override = raw.get("poll_seconds_override")
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
