from __future__ import annotations

from decimal import Decimal
from typing import Any

from .clients import AccountUsage, QuotaDisplay
from .state import CreditAlertKind, ModelRecord, PersistentState


_BUTTON_ICONS = {
    "Status": "📡",
    "Probe Now": "⚡",
    "Balance": "💰",
    "API Keys": "🔑",
    "Credit Alerts": "🔔",
    "Weekly Digest": "📊",
    "Schedule": "⏱️",
    "Help": "❓",
    "Key Alerts": "🔔",
    "Troubleshoot Key": "🛠️",
    "Previous Keys": "⬅️",
    "Next Keys": "➡️",
    "Back": "↩️",
}


def telegram_keyboard(*rows: tuple[str, ...], **options: bool) -> dict[str, Any]:
    return {
        "keyboard": [
            [
                {"text": f"{_BUTTON_ICONS[label]} {label}" if label in _BUTTON_ICONS else label}
                for label in row
            ]
            for row in rows
        ],
        "resize_keyboard": True,
        **options,
    }


def telegram_button_action(text: str) -> str:
    action = " ".join(text.lower().split())
    icon, _, label = action.partition(" ")
    return label if icon in _BUTTON_ICONS.values() else action


TELEGRAM_COMMANDS = [
    {"command": "status", "description": "📡 Latest model probes"},
    {"command": "probe_now", "description": "⚡ Probe every listed model"},
    {"command": "balance", "description": "💰 Balance and usage"},
    {"command": "api_keys", "description": "🔑 Key quotas, usage, and alerts"},
    {"command": "credit_alerts", "description": "🔔 Spending and balance alerts"},
    {"command": "weekly_digest", "description": "📊 Weekly spending report"},
    {"command": "probing_schedule", "description": "⏱️ Change the probe interval"},
    {"command": "help", "description": "❓ Quick guide"},
]
TELEGRAM_MENU = telegram_keyboard(
    ("Status", "Probe Now"),
    ("Balance", "API Keys"),
    ("Credit Alerts", "Weekly Digest"),
    ("Schedule", "Help"),
    is_persistent=True,
)
TELEGRAM_HELP = (
    "❓ AgentRouter Help\n\n"
    "📡 Status (/status) · ✅ available, ❌ unavailable, ❔ unknown\n"
    "⚡ Probe Now (/probe_now) · Test every listed model\n"
    "💰 Balance (/balance) · Wallet, total spend, requests, and group\n"
    "🔑 API Keys (/api_keys) · Quota bars [spent/left]. Pick a key for 24h/7d usage, "
    "spending and quota alerts, expiry reminders, and troubleshooting.\n"
    "🔔 Credit Alerts (/credit_alerts) · One-time spending and low-balance alerts\n"
    "📊 Weekly Digest (/weekly_digest) · Compare the last two completed UTC weeks. "
    "See top keys and models by spend or turn on auto-send.\n"
    "⏱️ Schedule (/probing_schedule) · Probe every 5, 15, or 30 minutes, or use the default\n\n"
    "Choose any AgentRouter feature to set up missing or rejected credentials.\n"
    "Automatic alerts cover model availability changes and low probing-key quota.\n"
    "Send /start to refresh the keyboard."
)
SCHEDULE_CHOICES: dict[str, int | None] = {
    **{f"every {minutes} min": minutes * 60 for minutes in (5, 15, 30)},
    "use default": None,
}
TELEGRAM_SCHEDULE_MENU = telegram_keyboard(
    tuple(label.capitalize() for label, seconds in SCHEDULE_CHOICES.items() if seconds is not None),
    ("Use default", "Back"),
    one_time_keyboard=True,
)
TELEGRAM_CREDIT_ALERT_MENU = telegram_keyboard(
    ("Spending Alert", "Low Balance Alert"), ("Back",), one_time_keyboard=True
)

MODEL_STATUS_ICONS = {
    "available": "✅",
    "unavailable": "❌",
    "unknown": "❔",
}


def model_probe_status(record: ModelRecord | None) -> str:
    if record is not None and not record.probe_error and record.availability in MODEL_STATUS_ICONS:
        return record.availability
    return "unknown"


def status_message(state: PersistentState) -> str:
    lines = [
        f"{MODEL_STATUS_ICONS[model_probe_status(state.models.get(model))]} {model}"
        for model in state.active_models
    ]
    return "📡 Model Status\n\n" + ("\n".join(lines) or "No models checked yet.")


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [
        f"{value} {unit}{'' if value == 1 else 's'}"
        for value, unit in ((hours, "hour"), (minutes, "minute"), (seconds, "second"))
        if value
    ]
    return " ".join(parts) or "0 seconds"


def probing_schedule_message(poll_seconds: int, probe_concurrency: int) -> str:
    return (
        "⏱️ Probing Schedule\n\n"
        f"Every {format_duration(poll_seconds)} · up to {probe_concurrency} probes at once\n"
        "One probe per model on the dashboard."
    )


def format_account_amount(usage: AccountUsage, quota: int) -> str:
    return format_quota_amount(quota, (usage.quota_per_unit, usage.display_in_currency))


def format_quota_amount(quota: int, display: QuotaDisplay) -> str:
    quota_per_unit, display_in_currency = display
    if display_in_currency and quota_per_unit > 0:
        amount = Decimal(quota) / Decimal(str(quota_per_unit))
        return f"${amount:,.2f}"
    return f"{quota:,} quota"


def probe_key_balance_message(balance: Decimal, threshold: Decimal) -> str:
    return (
        "⚠️ Low probing key balance\n\n"
        f"${balance:,.2f} left · ${threshold:,.2f} threshold"
    )


def account_usage_message(usage: AccountUsage) -> str:
    balance = (
        "Unlimited balance" if usage.quota < 0
        else f"{format_account_amount(usage, usage.quota)} available"
    )
    return (
        f"💰 {balance}\n\n"
        f"Total spent · {format_account_amount(usage, usage.used_quota)}\n"
        f"Requests · {usage.request_count:,}\n"
        f"Group · {usage.group or 'default'}"
    )


def credit_alert_confirmation(
    kind: CreditAlertKind, usage: AccountUsage, threshold: int
) -> str:
    target = format_account_amount(usage, threshold)
    used = format_account_amount(usage, usage.used_quota)
    if kind == "spending":
        additional = format_account_amount(usage, threshold - usage.used_quota)
        return (
            "🔔 Spending alert set\n\n"
            f"{used} spent · {additional} more\n"
            f"Alert at {target} total spent"
        )

    balance = format_account_amount(usage, usage.quota)
    summary = (
        "🔔 Low balance alert set\n\n"
        f"{balance} left · {used} spent\n"
        f"Alert when balance is below {target}"
    )
    if usage.quota < threshold:
        return f"{summary}\nAlert queued · already below threshold"

    additional_quota = usage.quota - threshold + 1
    additional = format_account_amount(usage, additional_quota)
    if additional == "$0.00":
        additional = "less than $0.01"
    projected = format_account_amount(usage, usage.used_quota + additional_quota)
    return f"{summary}\nEst. spend at alert · {projected} ({additional} more)"


def credit_alert_message(kind: CreditAlertKind, usage: AccountUsage, threshold: int) -> str:
    target = format_account_amount(usage, threshold)
    used = format_account_amount(usage, usage.used_quota)
    if kind == "spending":
        return f"🔔 Spending alert\n\n{used} total spent · {target} target"
    balance = format_account_amount(usage, usage.quota)
    return f"⚠️ Low balance alert\n\n{balance} left · {target} threshold\n{used} total spent"


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
    groups: dict[str, list[str]] = {status: [] for status in MODEL_STATUS_ICONS}
    for model in state.active_models:
        groups[model_probe_status(state.models.get(model))].append(model)
    return "\n\n".join(
        "\n".join([f"{MODEL_STATUS_ICONS[availability]} {availability.capitalize()}", *models])
        for availability, models in groups.items()
        if models
    )
