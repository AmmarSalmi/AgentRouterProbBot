import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentrouter_prober import app as prober_app
from agentrouter_prober.app import Prober
from agentrouter_prober.clients import (
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    TransientClientError,
)
from agentrouter_prober.config import Settings, TelegramSettings
from agentrouter_prober.state import StateStore, format_datetime


UTC = timezone.utc


def make_settings(path: Path) -> Settings:
    return Settings(
        agentrouter_base_url="https://agentrouter.test",
        agentrouter_api_key="sk-test",
        telegram=TelegramSettings("bot", "42", "https://telegram.test"),
        poll_seconds=900,
        probe_max_output_tokens=32,
        probe_concurrency=2,
        request_timeout_seconds=90,
        state_path=path,
        health_max_age_seconds=3600,
        agentrouter_dashboard_token="dashboard-token",
        agentrouter_user_id=1,
        client_user_agent="codex_cli_rs/test",
    )


def observation(name: str, apis=("responses",), status="operational"):
    return ModelObservation(name, status, "ok", apis)


class FakeAgentRouter:
    def __init__(self, statuses, outcomes):
        self.statuses = list(statuses)
        self.outcomes = list(outcomes)
        self.probed = []

    async def fetch_model_status(self):
        return self.statuses.pop(0)

    async def probe_model(self, model):
        self.probed.append(model.name)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return ModelProbe(outcome, model.apis[0], 100, "OK" if outcome is ProbeOutcome.AVAILABLE else None)

    async def close(self):
        return None


class FakeTelegram:
    send_retry_at = None

    def __init__(self):
        self.messages = []

    async def send(self, text, **kwargs):
        self.messages.append(text)

    async def close(self):
        return None


def test_changing_interval_reschedules_an_overdue_probe(state_directory, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(prober_app, "monotonic", lambda: clock[0])
    prober = Prober(
        make_settings(state_directory / "state.json"),
        FakeAgentRouter([], []),
        FakeTelegram(),
    )

    async def run():
        cycles = asyncio.Queue()
        stop = asyncio.Event()

        async def run_cycle():
            cycles.put_nowait(clock[0])

        monkeypatch.setattr(prober, "run_cycle", run_cycle)
        worker = asyncio.create_task(prober.run_forever(stop))
        try:
            assert await asyncio.wait_for(cycles.get(), timeout=1) == 0
            clock[0] = 600
            prober.set_poll_seconds(300)
            assert await asyncio.wait_for(cycles.get(), timeout=1) == 600
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=1)

    asyncio.run(run())


def test_every_status_model_is_probed_on_every_cycle_and_notified_once(state_directory):
    path = state_directory / "state.json"
    statuses = {
        "gpt-6.2-sol": observation("gpt-6.2-sol"),
        "gpt-5.6-sol": observation("gpt-5.6-sol"),
        "claude-opus-5-5": observation(
            "claude-opus-5-5", ("claude_native",), "no_traffic"
        ),
    }
    agent = FakeAgentRouter(
        [statuses, statuses],
        [
            ProbeOutcome.AVAILABLE,
            ProbeOutcome.AVAILABLE,
            ProbeOutcome.UNAVAILABLE,
            ProbeOutcome.AVAILABLE,
            ProbeOutcome.AVAILABLE,
            ProbeOutcome.UNAVAILABLE,
        ],
    )
    telegram = FakeTelegram()
    prober = Prober(make_settings(path), agent, telegram)
    now = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)

    first = asyncio.run(prober.run_cycle(now))
    second = asyncio.run(prober.run_cycle(now + timedelta(minutes=15)))

    assert agent.probed == [
        "gpt-6.2-sol",
        "gpt-5.6-sol",
        "claude-opus-5-5",
        "gpt-6.2-sol",
        "gpt-5.6-sol",
        "claude-opus-5-5",
    ]
    assert [first.notifications_sent, second.notifications_sent] == [1, 0]
    assert telegram.messages == [
        "Available:\ngpt-6.2-sol\ngpt-5.6-sol\n\nNot available:\nclaude-opus-5-5"
    ]
    saved = StateStore(path).load()
    assert saved.active_models == ["gpt-6.2-sol", "gpt-5.6-sol", "claude-opus-5-5"]
    assert saved.model("gpt-6.2-sol").availability == "available"
    assert saved.model("claude-opus-5-5").availability == "unavailable"
    assert all(not saved.model(model).notification_pending for model in saved.active_models)


def test_unavailable_then_available_sends_recovery_notification(state_directory):
    path = state_directory / "state.json"
    statuses = {"gpt": observation("gpt")}
    agent = FakeAgentRouter(
        [statuses, statuses, statuses],
        [ProbeOutcome.AVAILABLE, ProbeOutcome.UNAVAILABLE, ProbeOutcome.AVAILABLE],
    )
    telegram = FakeTelegram()
    prober = Prober(make_settings(path), agent, telegram)
    now = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)

    for offset in (0, 15, 30):
        asyncio.run(prober.run_cycle(now + timedelta(minutes=offset)))
    assert telegram.messages == [
        "Available:\ngpt", "Not available:\ngpt", "Available:\ngpt"
    ]


def test_all_unavailable_models_send_one_list_without_repeating(state_directory):
    path = state_directory / "state.json"
    statuses = {"first": observation("first"), "second": observation("second")}
    agent = FakeAgentRouter(
        [statuses, statuses], [ProbeOutcome.UNAVAILABLE] * 4
    )
    telegram = FakeTelegram()
    prober = Prober(make_settings(path), agent, telegram)
    asyncio.run(prober.run_cycle())
    asyncio.run(prober.run_cycle())
    assert telegram.messages == ["Not available:\nfirst\nsecond"]


def test_long_model_list_fits_telegram_messages_without_losing_models(state_directory):
    path = state_directory / "state.json"
    models = [f"test-model-{index:03d}-{'x' * 20}" for index in range(150)]
    statuses = {model: observation(model) for model in models}
    agent = FakeAgentRouter([statuses], [ProbeOutcome.AVAILABLE] * len(models))
    telegram = FakeTelegram()
    prober = Prober(make_settings(path), agent, telegram)
    result = asyncio.run(prober.run_cycle())
    assert result.notifications_sent == len(telegram.messages) > 1
    assert all(len(message) <= 4000 for message in telegram.messages)
    assert "\n".join(telegram.messages).splitlines() == ["Available:", *models]
    assert all(not prober.state.model(model).notification_pending for model in models)


def test_rate_limited_list_survives_restart_and_retries_after_deadline(
    state_directory, monkeypatch
):
    path = state_directory / "state.json"
    clock = [datetime(2026, 11, 21, 7, 0, tzinfo=UTC)]
    monkeypatch.setattr(prober_app, "utc_now", lambda: clock[0])
    statuses = {"ready": observation("ready"), "down": observation("down")}
    agent = FakeAgentRouter(
        [statuses] * 3,
        [ProbeOutcome.AVAILABLE, ProbeOutcome.UNAVAILABLE] * 3,
    )

    class RateLimitedTelegram(FakeTelegram):
        async def send(self, text, **kwargs):
            raise TransientClientError("Telegram rate limit reached", retry_after=60)

    prober = Prober(make_settings(path), agent, RateLimitedTelegram())
    first = asyncio.run(prober.run_cycle(clock[0]))
    saved = StateStore(path).load()
    assert first.notifications_sent == 0 and not first.fatal_error
    assert saved.telegram_retry_at == format_datetime(clock[0] + timedelta(seconds=60))
    assert all(saved.model(model).notification_pending for model in saved.active_models)

    telegram = FakeTelegram()
    restarted = Prober(make_settings(path), agent, telegram)
    clock[0] += timedelta(seconds=30)
    second = asyncio.run(restarted.run_cycle(clock[0]))
    assert second.notifications_sent == 0 and telegram.messages == []

    clock[0] += timedelta(seconds=30)
    third = asyncio.run(restarted.run_cycle(clock[0]))
    assert third.notifications_sent == 1
    assert telegram.messages == ["Available:\nready\n\nNot available:\ndown"]
    saved = StateStore(path).load()
    assert saved.telegram_retry_at is None
    assert all(not saved.model(model).notification_pending for model in saved.active_models)


def test_active_model_list_is_replaced_by_each_status_response(state_directory):
    path = state_directory / "state.json"
    agent = FakeAgentRouter(
        [
            {"old": observation("old")},
            {"new": observation("new")},
        ],
        [ProbeOutcome.UNAVAILABLE, ProbeOutcome.UNAVAILABLE],
    )
    prober = Prober(make_settings(path), agent, FakeTelegram())
    asyncio.run(prober.run_cycle())
    asyncio.run(prober.run_cycle())
    assert prober.state.active_models == ["new"]
    assert agent.probed == ["old", "new"]


def test_transient_probe_error_is_recorded_without_faking_unavailability(state_directory):
    path = state_directory / "state.json"
    agent = FakeAgentRouter(
        [{"gpt": observation("gpt"), "working": observation("working")}],
        [TransientClientError("temporary model error"), ProbeOutcome.AVAILABLE],
    )
    telegram = FakeTelegram()
    prober = Prober(make_settings(path), agent, telegram)
    result = asyncio.run(prober.run_cycle())
    record = prober.state.model("gpt")
    assert result.dashboard_ok
    assert record.availability == "unknown"
    assert record.probe_error == "temporary model error"
    assert telegram.messages == ["Available:\nworking\n\nUnknown:\ngpt"]
