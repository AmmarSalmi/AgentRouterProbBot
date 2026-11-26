from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from .clients import ApiKey, QuotaDisplay, UsageTotals
from .messages import format_quota_amount
from .state import KeyAlert, PersistentState


DAY_SECONDS = 86_400
WEEK_SECONDS = 7 * DAY_SECONDS


def completed_week_end(now: datetime) -> int:
    day = now.astimezone(timezone.utc).date()
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) - (
        day.weekday() * DAY_SECONDS
    )


def _date_label(timestamp: int, default: str) -> str:
    if timestamp <= 0:
        return default
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "Unknown"


def key_label(key: ApiKey) -> str:
    return f"{key.name} (#{key.id})"


def key_status(key: ApiKey, now: int) -> str:
    issues = []
    if key.status == 2:
        issues.append("Disabled")
    if key.status == 3 or 0 < key.expires_at <= now:
        issues.append("Expired")
    if key.status == 4 or (key.remaining_quota is not None and key.remaining_quota <= 0):
        issues.append("Out of quota")
    return ", ".join(issues) or ("Active" if key.status == 1 else "Unknown status")


def key_quota(key: ApiKey, display: QuotaDisplay) -> str:
    if key.remaining_quota is None:
        return "Unlimited"
    return format_quota_amount(key.remaining_quota, display)


def key_summary(key: ApiKey, display: QuotaDisplay, now: int) -> str:
    used = format_quota_amount(key.used_quota, display)
    return f"{key_label(key)} · {key_status(key, now)}\nQuota left: {key_quota(key, display)} · Total spent: {used}"


def key_details(key: ApiKey, display: QuotaDisplay, now: int) -> str:
    return "\n".join((
        key_summary(key, display, now),
        f"Last use: {_date_label(key.last_used_at, 'Never recorded')}",
        f"Expires: {_date_label(key.expires_at, 'Never')}",
        f"Group: {key.group or 'Account default'}",
        *(('This key runs the bot’s model probes.',) if key.is_probe_key else ()),
    ))


def _top_spending(items: Iterable[tuple[str, int]], display: QuotaDisplay) -> str:
    rows = [
        f"• {' '.join(name.split())[:120]} · {format_quota_amount(quota, display)}"
        for name, quota in items
    ]
    return "\n".join(rows) or "None"


def key_history_message(key: ApiKey, totals: UsageTotals, period: str, display: QuotaDisplay) -> str:
    return (
        f"{key_label(key)} · {period}\n\n"
        f"Spent: {format_quota_amount(totals.spent, display)}\n"
        f"Logged requests: {totals.requests:,}\n\n"
        f"Top models by spend\n{_top_spending(totals.by_model.most_common(5), display)}"
    )


def key_troubleshooting(key: ApiKey, state: PersistentState, now: int) -> str:
    if key.model_limits is None:
        models = "Any model available to the key’s group"
    else:
        models = ", ".join(key.model_limits) or "No models allowed"
    lines = [
        key_label(key),
        f"Status: {key_status(key, now)}",
        f"Expires: {_date_label(key.expires_at, 'Never')}",
        f"Group: {key.group or 'Account default'}",
        f"Model access: {models}",
        f"Allowed IPs: {', '.join(key.allowed_ips) or 'Any'}",
        "",
        "Latest live probes with the bot’s probing key",
    ]
    if state.last_cycle_at:
        lines.append(state.last_cycle_at)
    for name in state.active_models:
        record = state.models.get(name)
        result = record.availability.capitalize() if record and not record.probe_error else "Unknown"
        if key.model_limits is not None and name not in key.model_limits:
            result += " · Restricted for this key"
        lines.append(f"{name} · {result}")
    if not state.active_models:
        lines.append("No model probes recorded yet.")
    return "\n".join(lines)


def key_alert_message(key: ApiKey, alert: KeyAlert, display: QuotaDisplay) -> str:
    target = format_quota_amount(alert.threshold, display)
    if alert.kind == "spending":
        used = format_quota_amount(key.used_quota, display)
        return f"API key spending alert\n\n{key_label(key)} has spent {used}, reaching {target}."
    if alert.kind == "balance":
        return (
            f"API key low-quota alert\n\n{key_label(key)} has {key_quota(key, display)} "
            f"left, below {target}."
        )
    return f"API key expiry reminder\n\n{key_label(key)} expires {_date_label(key.expires_at, 'at an unknown time')}."


def key_alert_confirmation(key: ApiKey, alert: KeyAlert, display: QuotaDisplay) -> str:
    target = format_quota_amount(alert.threshold, display)
    if alert.kind == "spending":
        used = format_quota_amount(key.used_quota, display)
        detail = f"Total spent is {used}. You’ll be alerted when it reaches {target}."
    elif alert.kind == "balance":
        detail = f"Quota left is {key_quota(key, display)}. You’ll be alerted below {target}."
    else:
        expires = _date_label(key.expires_at, "at an unknown time")
        detail = f"The key expires {expires}. You’ll be reminded {alert.threshold} day(s) before expiry."
    if alert.pending_message:
        detail += " The alert is already due and has been queued."
    return f"Alert saved for {key_label(key)}.\n{detail}"


def weekly_usage_message(
    end: int, current: UsageTotals, previous: UsageTotals, display: QuotaDisplay
) -> str:
    current_spent = format_quota_amount(current.spent, display)
    previous_spent = format_quota_amount(previous.spent, display)
    change = current.spent - previous.spent
    comparison = "Same spending as the prior week."
    if change:
        amount = format_quota_amount(abs(change), display)
        comparison = f"{amount} {'more' if change > 0 else 'less'} than the prior week."
    first_day = _date_label(end - WEEK_SECONDS, "Unknown").split()[0]
    last_day = _date_label(end - 1, "Unknown").split()[0]
    keys = [
        (f"{current.key_names.get(key_id, 'Key')} (#{key_id})" if key_id > 0 else "Unattributed", quota)
        for key_id, quota in current.by_key.most_common(5)
    ]
    return (
        f"Weekly spending digest\n{first_day} to {last_day} UTC\n\n"
        f"Spent: {current_spent} · Prior week: {previous_spent}\n"
        f"{comparison}\nLogged requests: {current.requests:,}\n\n"
        f"Top keys by spend\n{_top_spending(keys, display)}\n\n"
        f"Top models by spend\n{_top_spending(current.by_model.most_common(5), display)}"
    )
