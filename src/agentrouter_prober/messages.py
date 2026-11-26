from __future__ import annotations

from datetime import datetime

from .clients import AccountUsage
from .config import Settings
from .state import ModelRecord, PersistentState


TELEGRAM_COMMANDS = [
    {"command": "status", "description": "Show live model probes"},
    {"command": "balance", "description": "Show balance and usage"},
    {"command": "probe_now", "description": "Probe every listed model now"},
    {"command": "set_model", "description": "Choose the Codex default model"},
    {"command": "probing_schedule", "description": "Show the probing schedule"},
]
TELEGRAM_MENU = {
    "keyboard": [
        [{"text": "Status"}, {"text": "Probe Now"}],
        [{"text": "Balance & Usage"}, {"text": "Set Codex Model"}],
        [{"text": "Probing Schedule"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def status_message(settings: Settings, state: PersistentState) -> str:
    lines = []
    for model in state.active_models:
        record = state.models.get(model, ModelRecord())
        available = record.availability == "available" and not record.probe_error
        lines.append(f"{model} {'✅' if available else '❌'}")
    return "\n".join(lines) or "No models checked yet."


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [
        f"{value} {unit}{'' if value == 1 else 's'}"
        for value, unit in ((hours, "hour"), (minutes, "minute"), (seconds, "second"))
        if value
    ]
    return " ".join(parts) or "0 seconds"


def probing_schedule_message(settings: Settings, now: datetime | None = None) -> str:
    return "\n".join(
        [
            "Probing Schedule",
            "",
            f"Every {_duration(settings.poll_seconds)}, the bot:",
            "1. Reads the AgentRouter dashboard's model list.",
            "2. Sends one minimal greeting probe to every model listed there.",
            "",
            "Models from the documentation or pricing page are never added automatically.",
            f"At most {settings.probe_concurrency} model probes run at once.",
        ]
    )


def account_usage_message(usage: AccountUsage) -> str:
    def amount(quota: int) -> str:
        if usage.display_in_currency and usage.quota_per_unit > 0:
            return f"${quota / usage.quota_per_unit:,.2f}"
        return f"{quota:,} quota"

    balance = "Unlimited" if usage.quota < 0 else amount(usage.quota)
    return "\n".join(
        [
            "AgentRouter balance",
            "",
            f"Current balance: {balance}",
            f"Total consumed: {amount(usage.used_quota)}",
            f"Requests: {usage.request_count:,}",
            f"Group: {usage.group or 'default'}",
        ]
    )


def telegram_message_parts(text: str, limit: int = 4000) -> list[str]:
    parts = []
    while len(text) > limit:
        split = text.rfind("\n", 0, limit + 1)
        if split <= 0:
            split = limit
        parts.append(text[:split])
        text = text[split:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def availability_message(state: PersistentState) -> str:
    groups: dict[str, list[str]] = {
        "available": [],
        "unavailable": [],
        "unknown": [],
    }
    for model in state.active_models:
        record = state.models.get(model, ModelRecord())
        availability = "unknown" if record.probe_error else record.availability
        groups.get(availability, groups["unknown"]).append(model)

    headings = {
        "available": "Available:",
        "unavailable": "Not available:",
        "unknown": "Unknown:",
    }
    return "\n\n".join(
        "\n".join([headings[availability], *models])
        for availability, models in groups.items()
        if models
    )
