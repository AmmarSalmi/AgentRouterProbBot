import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentrouter_prober import app as prober_app
from agentrouter_prober.app import Prober
from agentrouter_prober.clients import (
    AccountUsage,
    ApiKey,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    TransientClientError,
    UsageTotals,
)
from agentrouter_prober.config import Settings, TelegramSettings
from agentrouter_prober.key_messages import WEEK_SECONDS, completed_week_end
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
    def __init__(self, statuses, outcomes, account_usages=(), probe_key_balances=()):
        self.statuses = list(statuses)
        self.outcomes = list(outcomes)
        self.account_usages = list(account_usages)
        self.probe_key_balances = list(probe_key_balances)
        self.account_calls = 0
        self.probed = []

    async def fetch_model_status(self):
        status = self.statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        return status

    async def fetch_account_usage(self):
        self.account_calls += 1
        usage = self.account_usages.pop(0)
        if isinstance(usage, Exception):
            raise usage
        return usage

    async def fetch_api_keys(self):
        balance = self.probe_key_balances.pop(0) if self.probe_key_balances else None
        if isinstance(balance, Exception):
            raise balance
        remaining = None if balance is None else int(balance * 500_000)
        return {1: ApiKey(1, "Prober", 1, remaining, 0, is_probe_key=True)}

    async def quota_display_settings(self):
        return 500_000.0, True

    async def probe_model(self, model):
        self.probed.append(model.name)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return ModelProbe(outcome, model.apis[0], 100, "OK" if outcome is ProbeOutcome.AVAILABLE else None)

    async def close(self):
        return None


class FakeTelegram:
    chat_id = "42"
    send_retry_at = None

    def __init__(self):
        self.messages = []

    async def send(self, text, **kwargs):
        self.messages.append(text)

    async def close(self):
        return None


def account_usage(balance: int, used: int) -> AccountUsage:
    return AccountUsage(balance, used, 0, "", 500_000.0, True)


def test_credit_alerts_survive_restart_and_trigger_once_from_live_usage(state_directory):
    settings = make_settings(state_directory / "state.json")
    initial = account_usage(6_250_000, 2_500_000)
    setup_agent = FakeAgentRouter([], [], [initial, initial])
    telegram = FakeTelegram()

    async def run():
        prober = Prober(settings, setup_agent, telegram)
        await prober.set_credit_alert("spending", Decimal("2.50"))
        await prober.set_credit_alert("balance", Decimal("10"))

        monitor = FakeAgentRouter(
            [{}] * 6,
            [],
            [
                TransientClientError("Account service unavailable"),
                account_usage(5_000_000, 3_749_999),
                account_usage(10_000_000, 3_750_000),
                account_usage(-1, 4_000_000),
                account_usage(4_750_000, 9_000_000),
            ],
        )
        restarted = Prober(settings, monitor, telegram)
        sent = [(await restarted.run_cycle()).notifications_sent for _ in range(6)]
        assert sent == [0, 0, 1, 0, 1, 0]
        assert monitor.account_calls == 5

    asyncio.run(run())
    assert telegram.messages == [
        "🔔 Spending alert\n\n$7.50 total spent · $7.50 target",
        "⚠️ Low balance alert\n\n$9.50 left · $10.00 threshold\n$18.00 total spent",
    ]
    assert StateStore(settings.state_path).load().credit_alerts == {}


def test_probe_key_warning_retries_and_rearms_after_quota_recovers(state_directory):
    settings = make_settings(state_directory / "state.json")

    class UnavailableTelegram(FakeTelegram):
        async def send(self, text, **kwargs):
            raise TransientClientError("Telegram temporarily unavailable")

    async def run():
        agent = FakeAgentRouter(
            [{}] * 6,
            [],
            probe_key_balances=[
                Decimal("0.75"),
                TransientClientError("Key quota temporarily unavailable"),
                Decimal("0.50"),
                Decimal("1.00"),
                Decimal("0"),
                None,
            ],
        )
        prober = Prober(settings, agent, UnavailableTelegram())
        assert (await prober.run_cycle()).notifications_sent == 0

        telegram = FakeTelegram()
        for expected in (1, 0, 0, 1, 0):
            restarted = Prober(settings, agent, telegram)
            result = await restarted.run_cycle()
            assert result.notifications_sent == expected
            assert not result.fatal_error
        assert len(telegram.messages) == 2
        assert "$0.75 left" in telegram.messages[0]
        assert "$0.00 left" in telegram.messages[1]
        assert all("$1.00 threshold" in message for message in telegram.messages)
        assert StateStore(settings.state_path).load().probe_key_alert is None

    asyncio.run(run())


def test_probe_key_warning_uses_quota_units_for_quota_accounts(state_directory):
    class QuotaAgentRouter(FakeAgentRouter):
        async def quota_display_settings(self):
            return 500_000.0, False

    settings = make_settings(state_directory / "state.json")
    agent = QuotaAgentRouter([{}], [], probe_key_balances=[Decimal("0.75")])
    telegram = FakeTelegram()
    asyncio.run(Prober(settings, agent, telegram).run_cycle())

    assert telegram.messages == [
        "⚠️ Low probing key balance\n\n375,000 quota left · 500,000 quota threshold"
    ]


def test_pending_credit_alert_retries_for_its_recipient_after_restart(
    state_directory, monkeypatch
):
    settings = make_settings(state_directory / "state.json")
    clock = [datetime(2026, 11, 21, 7, 0, tzinfo=UTC)]
    monkeypatch.setattr(prober_app, "utc_now", lambda: clock[0])

    class RateLimitedTelegram(FakeTelegram):
        async def send(self, text, **kwargs):
            raise TransientClientError("Telegram rate limit reached", retry_after=60)

    async def run():
        agent = FakeAgentRouter(
            [TransientClientError("Model-status unavailable")],
            [],
            [account_usage(6_250_000, 2_500_000), account_usage(4_750_000, 4_000_000)],
        )
        prober = Prober(settings, agent, RateLimitedTelegram())
        await prober.set_credit_alert("balance", Decimal("10"))
        result = await prober.run_cycle()
        assert not result.dashboard_ok and not result.fatal_error
        assert result.notifications_sent == 0
        saved = StateStore(settings.state_path).load()
        assert "$9.50" in saved.credit_alerts["balance"].pending_message
        assert saved.telegram_retry_at == format_datetime(clock[0] + timedelta(seconds=60))

        monitor = FakeAgentRouter([{}, {}, {}], [])
        telegram = FakeTelegram()
        restarted = Prober(settings, monitor, telegram)
        clock[0] += timedelta(seconds=30)
        assert (await restarted.run_cycle()).notifications_sent == 0
        clock[0] += timedelta(seconds=30)

        other_chat = FakeTelegram()
        other_chat.chat_id = "-100"
        migrated = Prober(settings, monitor, other_chat)
        assert (await migrated.run_cycle()).notifications_sent == 0
        assert other_chat.messages == []

        assert (await restarted.run_cycle()).notifications_sent == 1
        assert telegram.messages == [
            "⚠️ Low balance alert\n\n$9.50 left · $10.00 threshold\n$8.00 total spent"
        ]
        assert monitor.account_calls == 0
        assert StateStore(settings.state_path).load().credit_alerts == {}

    asyncio.run(run())


def test_selected_keys_alert_once_and_digest_retries_after_restart(state_directory, monkeypatch):
    settings = make_settings(state_directory / "state.json")
    monday = datetime(2026, 11, 23, tzinfo=UTC)
    clock = [monday - timedelta(days=3)]
    monkeypatch.setattr(prober_app, "utc_now", lambda: clock[0])

    class KeyAgent(FakeAgentRouter):
        keys = {
            4: ApiKey(4, "Coding", 1, 900_000, 500_000),
            5: ApiKey(5, "Coding", 1, None, 1_000_000, expires_at=int((monday + timedelta(days=2)).timestamp())),
            8: ApiKey(8, "Prober", 1, None, 0, is_probe_key=True),
        }

        async def fetch_api_keys(self):
            return self.keys.copy()

        async def fetch_usage_totals(self, start, end):
            self.windows.append((start, end))
            if end == completed_week_end(monday):
                return UsageTotals(2_000_000, 4, Counter({4: 2_000_000}), Counter({"alpha": 2_000_000}), {4: "Coding"})
            return UsageTotals(500_000, 2)

    class RateLimitedTelegram(FakeTelegram):
        async def send(self, text, **kwargs):
            raise TransientClientError("Telegram rate limit reached", retry_after=60)

    async def run():
        agent = KeyAgent([{}] * 5, [])
        agent.windows = []
        prober = Prober(settings, agent, RateLimitedTelegram())
        await prober.set_key_alert(4, "spending", Decimal("1.00"))
        await prober.set_key_alert(4, "balance", Decimal("1.00"))
        await prober.set_key_alert(5, "expiry", Decimal(2))
        prober.set_weekly_digest(True)
        assert (await prober.run_cycle()).notifications_sent == 0
        assert agent.windows == []

        clock[0] = monday
        agent.keys[4] = replace(agent.keys[4], name="Renamed", used_quota=1_000_000, remaining_quota=400_000)
        assert (await prober.run_cycle()).notifications_sent == 0
        saved = StateStore(settings.state_path).load()
        assert len(saved.key_monitoring.key_alerts) == 3
        assert all(alert.pending_message for alert in saved.key_monitoring.key_alerts.values())
        assert saved.key_monitoring.weekly_digest.pending_message

        clock[0] += timedelta(seconds=60)
        group = FakeTelegram()
        group.chat_id = "-100"
        assert (await Prober(settings, agent, group).run_cycle()).notifications_sent == 0
        assert group.messages == []
        telegram = FakeTelegram()
        restarted = Prober(settings, agent, telegram)
        assert (await restarted.run_cycle()).notifications_sent == 4
        assert (await restarted.run_cycle()).notifications_sent == 0
        assert "Renamed (#4)\n\n$2.00 total spent · $2.00 target" in telegram.messages[0]
        assert "Renamed (#4)\n\n$0.80 left · $1.00 threshold" in telegram.messages[1]
        assert "Coding (#5)\n\nExpires · 2026-11-25" in telegram.messages[2]
        assert "$4.00 spent · 4 logged requests\n↑ $3.00 vs prior week ($1.00)" in telegram.messages[3]
        assert "Coding (#4) · $4.00" in telegram.messages[3] and "alpha · $4.00" in telegram.messages[3]
        end = int(monday.timestamp())
        assert agent.windows == [(end - WEEK_SECONDS, end), (end - WEEK_SECONDS * 2, end - WEEK_SECONDS)]
        saved = StateStore(settings.state_path).load().key_monitoring
        assert saved.key_alerts == {}
        assert saved.weekly_digest.week_end == end and saved.weekly_digest.pending_message is None

    asyncio.run(run())


def test_replacing_alert_during_delivery_keeps_the_new_threshold(state_directory):
    settings = make_settings(state_directory / "state.json")
    usage = account_usage(4_750_000, 4_000_000)
    agent = FakeAgentRouter([{}], [], [usage, usage])

    async def run():
        sending = asyncio.Event()
        finish = asyncio.Event()

        class DelayedTelegram(FakeTelegram):
            async def send(self, text, **kwargs):
                sending.set()
                await finish.wait()
                await super().send(text, **kwargs)

        prober = Prober(settings, agent, DelayedTelegram())
        await prober.set_credit_alert("balance", Decimal("10"))
        cycle = asyncio.create_task(prober.run_cycle())
        try:
            await asyncio.wait_for(sending.wait(), timeout=1)
            await prober.set_credit_alert("balance", Decimal("6"))
        finally:
            finish.set()
            await asyncio.wait_for(cycle, timeout=1)

    asyncio.run(run())
    alert = StateStore(settings.state_path).load().credit_alerts["balance"]
    assert alert.threshold_quota == 3_000_000 and alert.pending_message is None


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
        "✅ Available\ngpt-6.2-sol\ngpt-5.6-sol\n\n❌ Unavailable\nclaude-opus-5-5"
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
        "✅ Available\ngpt", "❌ Unavailable\ngpt", "✅ Available\ngpt"
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
    assert telegram.messages == ["❌ Unavailable\nfirst\nsecond"]


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
    assert "\n".join(telegram.messages).splitlines() == ["✅ Available", *models]
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
    assert telegram.messages == ["✅ Available\nready\n\n❌ Unavailable\ndown"]
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
    assert telegram.messages == ["✅ Available\nworking\n\n❔ Unknown\ngpt"]
