from datetime import datetime, time, timezone

from agentrouter_prober.schedule import in_window, next_poll_delay


UTC = timezone.utc


def test_in_window():
    assert in_window(time(2, 0), time(1, 50), time(2, 30))
    assert not in_window(time(3, 0), time(1, 50), time(2, 30))
    assert in_window(time(23, 30), time(23, 0), time(1, 0))
    assert in_window(time(0, 30), time(23, 0), time(1, 0))


def test_wakes_at_fast_window_boundary():
    now = datetime(2026, 11, 8, 1, 47, tzinfo=UTC)
    assert next_poll_delay(
        now,
        normal_seconds=900,
        fast_seconds=120,
        fast_start=time(1, 50),
        fast_end=time(2, 30),
    ) == 180


def test_uses_fast_interval_inside_window():
    now = datetime(2026, 11, 8, 2, 0, tzinfo=UTC)
    assert next_poll_delay(
        now,
        normal_seconds=900,
        fast_seconds=120,
        fast_start=time(1, 50),
        fast_end=time(2, 30),
    ) == 120

