from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from .clients import ApiKey, QuotaDisplay, UsageTotals
from .messages import MODEL_STATUS_ICONS, format_quota_amount, model_probe_status
from .state import KeyAlert, PersistentState


DAY_SECONDS = 86_400
WEEK_SECONDS = 7 * DAY_SECONDS
QUOTA_BAR_WIDTH = 10


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


def key_label(key: ApiKey, *, max_length: int | None = None) -> str:
    suffix = f" (#{key.id})"
    name = key.name
    if max_length is not None and len(name) + len(suffix) > max_length:
        name = f"{name[:max_length - len(suffix) - 1].rstrip()}…"
    return name + suffix


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


def _key_status_line(key: ApiKey, now: int) -> str:
    status = key_status(key, now)
    label = key_label(key)
    return f"✅ {label}" if status == "Active" else f"❌ {label} · {status}"


def _key_quota_bar(key: ApiKey, display: QuotaDisplay) -> str:
    used = format_quota_amount(key.used_quota, display)
    remaining = key.remaining_quota
    if remaining is None:
        return f"[{used}/∞] · Unlimited"
    total = key.used_quota + max(0, remaining)
    filled = (QUOTA_BAR_WIDTH * key.used_quota + total // 2) // total if total else 0
    if remaining > 0:
        # Keep one segment empty while the key still has quota.
        filled = min(QUOTA_BAR_WIDTH - 1, filled)
    bar = "█" * filled + "░" * (QUOTA_BAR_WIDTH - filled)
    return f"{bar} [{used}/{key_quota(key, display)}]"


def key_summary(key: ApiKey, display: QuotaDisplay, now: int) -> str:
    return f"{_key_status_line(key, now)}\n{_key_quota_bar(key, display)}"


def key_details(key: ApiKey, display: QuotaDisplay, now: int) -> str:
    return "\n".join((
        key_summary(key, display, now),
        "[spent/left]",
        "",
        f"Last use · {_date_label(key.last_used_at, 'Never recorded')}",
        f"Expires · {_date_label(key.expires_at, 'Never')}",
        f"Group · {key.group or 'Account default'}",
        *(("⚡ Used for model probes",) if key.is_probe_key else ()),
    ))


def _top_spending(items: Iterable[tuple[str, int]], display: QuotaDisplay) -> str:
    rows = [
        f"• {' '.join(name.split())[:120]} · {format_quota_amount(quota, display)}"
        for name, quota in items
    ]
    return "\n".join(rows) or "None"


def _usage_line(totals: UsageTotals, display: QuotaDisplay) -> str:
    spent = format_quota_amount(totals.spent, display)
    request = "request" if totals.requests == 1 else "requests"
    return f"{spent} spent · {totals.requests:,} logged {request}"


def key_history_message(key: ApiKey, totals: UsageTotals, period: str, display: QuotaDisplay) -> str:
    return (
        f"📊 {period}\n{key_label(key)}\n\n"
        f"{_usage_line(totals, display)}\n\n"
        f"Top models by spend\n{_top_spending(totals.by_model.most_common(5), display)}"
    )


def key_troubleshooting(key: ApiKey, state: PersistentState, now: int) -> str:
    if key.model_limits is None:
        models = "Any in this key’s group"
    else:
        models = ", ".join(key.model_limits) or "No models allowed"
    lines = [
        "🛠️ Troubleshoot Key",
        _key_status_line(key, now),
        "",
        f"Expires · {_date_label(key.expires_at, 'Never')}",
        f"Group · {key.group or 'Account default'}",
        f"Model access · {models}",
        f"Allowed IPs · {', '.join(key.allowed_ips) or 'Any'}",
        "",
        "📡 Latest probes · Bot key",
    ]
    if state.last_cycle_at:
        lines.append(state.last_cycle_at)
    for name in state.active_models:
        icon = MODEL_STATUS_ICONS[model_probe_status(state.models.get(name))]
        line = f"{icon} {name}"
        if key.model_limits is not None and name not in key.model_limits:
            line += " · 🔒 Restricted"
        lines.append(line)
    if not state.active_models:
        lines.append("No model probes recorded yet.")
    return "\n".join(lines)


def key_alert_message(key: ApiKey, alert: KeyAlert, display: QuotaDisplay) -> str:
    target = format_quota_amount(alert.threshold, display)
    label = key_label(key)
    if alert.kind == "spending":
        used = format_quota_amount(key.used_quota, display)
        return f"🔔 API key spending alert\n{label}\n\n{used} total spent · {target} target"
    if alert.kind == "balance":
        return f"⚠️ API key low-quota alert\n{label}\n\n{key_quota(key, display)} left · {target} threshold"
    return f"📅 API key expiry reminder\n{label}\n\nExpires · {_date_label(key.expires_at, 'Unknown')}"


def key_alert_confirmation(key: ApiKey, alert: KeyAlert, display: QuotaDisplay) -> str:
    target = format_quota_amount(alert.threshold, display)
    if alert.kind == "spending":
        used = format_quota_amount(key.used_quota, display)
        detail = f"{used} spent · alert at {target} total"
    elif alert.kind == "balance":
        detail = f"{key_quota(key, display)} left · alert below {target}"
    else:
        expires = _date_label(key.expires_at, "Unknown")
        days = f"{alert.threshold} {'day' if alert.threshold == 1 else 'days'}"
        detail = f"Expires · {expires}\nReminder · {days} before expiry"
    if alert.pending_message:
        detail += "\nAlert queued · already due"
    return f"🔔 Key alert set\n{key_label(key)}\n\n{detail}"


def weekly_usage_message(
    end: int, current: UsageTotals, previous: UsageTotals, display: QuotaDisplay
) -> str:
    previous_spent = format_quota_amount(previous.spent, display)
    change = current.spent - previous.spent
    comparison = f"→ Same as prior week ({previous_spent})"
    if change:
        amount = format_quota_amount(abs(change), display)
        comparison = f"{'↑' if change > 0 else '↓'} {amount} vs prior week ({previous_spent})"
    first_day = _date_label(end - WEEK_SECONDS, "Unknown").split()[0]
    last_day = _date_label(end - 1, "Unknown").split()[0]
    keys = [
        (f"{current.key_names.get(key_id, 'Key')} (#{key_id})" if key_id > 0 else "Unattributed", quota)
        for key_id, quota in current.by_key.most_common(5)
    ]
    return (
        f"📊 Weekly Digest\n{first_day} – {last_day} UTC\n\n"
        f"{_usage_line(current, display)}\n{comparison}\n\n"
        f"Top keys by spend\n{_top_spending(keys, display)}\n\n"
        f"Top models by spend\n{_top_spending(current.by_model.most_common(5), display)}"
    )
