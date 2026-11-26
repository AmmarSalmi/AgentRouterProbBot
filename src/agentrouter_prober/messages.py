from __future__ import annotations

from decimal import Decimal

from .clients import AccountUsage
from .config import Settings
from .state import CreditAlertKind, ModelRecord, PersistentState


TELEGRAM_COMMANDS = [
    {"command": "status", "description": "Show live model probes"},
    {"command": "balance", "description": "Show balance and usage"},
    {"command": "probe_now", "description": "Probe every listed model now"},
    {"command": "probing_schedule", "description": "View or change the probing interval"},
    {"command": "credit_alerts", "description": "Set spending and balance alerts"},
    {"command": "help", "description": "Show a quick guide to the bot"},
]
TELEGRAM_MENU = {
    "keyboard": [
        [{"text": "Status"}, {"text": "Probe Now"}],
        [{"text": "Balance & Usage"}, {"text": "Probing Schedule"}],
        [{"text": "Credit Alerts"}, {"text": "Help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
TELEGRAM_HELP = (
    "The bot probes AgentRouter models automatically and sends alerts when "
    "availability changes or the probing key is low on quota.\n\n"
    "• Status (/status) shows the latest model availability results.\n"
    "• Probe Now (/probe_now) refreshes the model list and probes every listed model immediately.\n"
    "• Balance & Usage (/balance) shows your balance, total usage, request count, and account group.\n"
    "• Probing Schedule (/probing_schedule) sets automatic probes to run every 5, 15, "
    "or 30 minutes or restores the default interval.\n"
    "• Credit Alerts (/credit_alerts) sets one-time alerts for additional spending or a low balance.\n\n"
    "Send /start to refresh the keyboard."
)
SCHEDULE_CHOICES: dict[str, int | None] = {
    **{f"every {minutes} min": minutes * 60 for minutes in (5, 15, 30)},
    "use default": None,
}
TELEGRAM_SCHEDULE_MENU = {
    "keyboard": [
        [
            {"text": label.capitalize()}
            for label, seconds in SCHEDULE_CHOICES.items()
            if seconds is not None
        ],
        [{"text": "Use default"}, {"text": "Back"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
TELEGRAM_CREDIT_ALERT_MENU = {
    "keyboard": [
        [{"text": "Spending Alert"}, {"text": "Low Balance Alert"}],
        [{"text": "Back"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}


def status_message(settings: Settings, state: PersistentState) -> str:
    lines = []
    for model in state.active_models:
        record = state.models.get(model, ModelRecord())
        available = record.availability == "available" and not record.probe_error
        lines.append(f"{model} {'✅' if available else '❌'}")
    return "\n".join(lines) or "No models checked yet."


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
        "Probing Schedule\n\n"
        f"Every {format_duration(poll_seconds)}, the bot reads the AgentRouter dashboard's "
        "model list and sends one greeting probe to every model listed there.\n"
        f"At most {probe_concurrency} model probes run at once.\n\n"
        "Choose an interval below."
    )


def format_account_amount(usage: AccountUsage, quota: int) -> str:
    if usage.display_in_currency and usage.quota_per_unit > 0:
        amount = Decimal(quota) / Decimal(str(usage.quota_per_unit))
        return f"${amount:,.2f}"
    return f"{quota:,} quota"


def probe_key_balance_message(balance: Decimal, threshold: Decimal) -> str:
    return (
        "Low probing key balance\n\n"
        f"The API key used for model probes has about ${balance:,.2f} in quota left. "
        f"The warning threshold is ${threshold:,.2f}."
    )


def account_usage_message(usage: AccountUsage) -> str:
    balance = "Unlimited" if usage.quota < 0 else format_account_amount(usage, usage.quota)
    return "\n".join(
        [
            "AgentRouter balance",
            "",
            f"Current balance: {balance}",
            f"Total consumed: {format_account_amount(usage, usage.used_quota)}",
            f"Requests: {usage.request_count:,}",
            f"Group: {usage.group or 'default'}",
        ]
    )


def credit_alert_confirmation(
    kind: CreditAlertKind, usage: AccountUsage, threshold: int
) -> str:
    target = format_account_amount(usage, threshold)
    used = format_account_amount(usage, usage.used_quota)
    if kind == "spending":
        additional = format_account_amount(usage, threshold - usage.used_quota)
        return (
            f"Spending alert set for {additional} more.\n"
            f"You have used {used} so far.\n"
            f"You’ll be alerted at {target} in total usage."
        )

    balance = format_account_amount(usage, usage.quota)
    summary = (
        f"Low-balance alert set for balances below {target}.\n"
        f"Your balance is {balance} and you have used {used} so far."
    )
    if usage.quota < threshold:
        return f"{summary}\nYour balance is already below the threshold. The alert is queued."

    additional_quota = usage.quota - threshold + 1
    additional = format_account_amount(usage, additional_quota)
    if additional == "$0.00":
        additional = "less than $0.01"
    projected = format_account_amount(usage, usage.used_quota + additional_quota)
    return (
        f"{summary}\nAt your current balance, expect the alert at about {projected} "
        f"in total usage ({additional} more)."
    )


def credit_alert_message(kind: CreditAlertKind, usage: AccountUsage, threshold: int) -> str:
    target = format_account_amount(usage, threshold)
    used = format_account_amount(usage, usage.used_quota)
    if kind == "spending":
        return f"Spending alert\n\nYou’ve used {used} in total, reaching your {target} target."
    balance = format_account_amount(usage, usage.quota)
    return f"Low balance alert\n\nYour balance is {balance}, below {target}. Total used is {used}."


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
