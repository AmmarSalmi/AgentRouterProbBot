from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone


UTC = timezone.utc


def in_window(value: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= value < end
    return value >= start or value < end


def next_poll_delay(
    now: datetime,
    *,
    normal_seconds: int,
    fast_seconds: int,
    fast_start: time,
    fast_end: time,
) -> int:
    """Return a delay that also wakes exactly at an upcoming window boundary."""
    now = now.astimezone(UTC)
    interval = (
        fast_seconds
        if in_window(now.time().replace(tzinfo=None), fast_start, fast_end)
        else normal_seconds
    )

    boundaries: list[datetime] = []
    for day_offset in (0, 1, 2):
        date = (now + timedelta(days=day_offset)).date()
        boundaries.append(datetime.combine(date, fast_start, tzinfo=UTC))
        boundaries.append(datetime.combine(date, fast_end, tzinfo=UTC))

    future_boundaries = [
        (boundary - now).total_seconds()
        for boundary in boundaries
        if boundary > now
    ]
    until_boundary = min(future_boundaries, default=float(interval))
    return max(1, math.ceil(min(float(interval), until_boundary)))

