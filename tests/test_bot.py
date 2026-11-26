import asyncio
from dataclasses import replace
from pathlib import Path

from agentrouter_prober.app import Prober
from agentrouter_prober.bot import TelegramCommands
from agentrouter_prober.clients import (
    AccountUsage,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    ProtocolError,
    TelegramBot,
)
from agentrouter_prober.config import Settings, TelegramSettings
from agentrouter_prober.state import StateStore


def make_settings(directory: Path) -> Settings:
    return Settings(
        agentrouter_base_url="https://agentrouter.test",
        agentrouter_api_key="sk-test",
        telegram=TelegramSettings("bot", "42", "https://telegram.test"),
        poll_seconds=900,
        probe_max_output_tokens=32,
        probe_concurrency=2,
        request_timeout_seconds=90,
        state_path=directory / "state.json",
        health_max_age_seconds=3600,
        agentrouter_dashboard_token="dashboard-token",
        agentrouter_user_id=1,
        client_user_agent="codex_cli_rs/test",
    )


def message(text, user_id=42, chat_id=42, chat_type="private", is_bot=False):
    return {
        "update_id": 1,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot},
        },
    }


class FakeAgent:
    def __init__(self):
        self.status_calls = 0
        self.account_calls = 0
        self.probed = []
        self.observations = {
            "gpt-6.1-sol": ModelObservation(
                "gpt-6.1-sol", "degraded", "warn", ("chat", "responses"),
                success_rate_24h=97.96, ttft_p90_ms_24h=900, avg_latency_ms=2000,
            ),
            "gpt-6.2-sol": ModelObservation(
                "gpt-6.2-sol", "no_traffic", "none", ("responses", "chat")
            ),
            "claude-opus-5-5": ModelObservation(
                "claude-opus-5-5", "no_traffic", "none", ("claude_native",)
            ),
        }

    async def fetch_model_status(self):
        self.status_calls += 1
        return self.observations

    async def fetch_account_usage(self):
        self.account_calls += 1
        return AccountUsage(6250000, 2500000, 321, "vip", 500000.0, True)

    async def probe_model(self, observation):
        self.probed.append(observation.name)
        available = observation.name == "gpt-6.1-sol"
        return ModelProbe(
            ProbeOutcome.AVAILABLE if available else ProbeOutcome.UNAVAILABLE,
            observation.apis[0],
            100,
            "OK" if available else None,
        )


class FakeTelegram:
    chat_id = "42"
    bot = TelegramBot(123, "prober_bot")
    send_retry_at = None

    def __init__(self):
        self.sent = []

    async def send(self, text, *, reply_markup=None):
        self.sent.append((text, reply_markup))


def setup(directory):
    settings = make_settings(directory)
    agent, telegram = FakeAgent(), FakeTelegram()
    return settings, agent, telegram, TelegramCommands(
        Prober(settings, agent, telegram)
    )


def test_status_discovers_and_probes_all_models_once(state_directory):
    settings, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Status")))
    assert agent.status_calls == 1
    assert agent.probed == list(agent.observations)
    assert telegram.sent[-1][0] == (
        "gpt-6.1-sol ✅\ngpt-6.2-sol ❌\nclaude-opus-5-5 ❌"
    )
    record = StateStore(settings.state_path).load().model("gpt-6.1-sol")
    assert record.dashboard_status == "degraded" and record.dashboard_tier == "warn"
    assert record.apis == ["chat", "responses"]
    assert record.success_rate_24h == 97.96
    assert record.ttft_p90_ms_24h == 900 and record.avg_latency_ms == 2000
    assert record.probe_api == "chat" and record.probe_latency_ms == 100
    assert record.probe_reply == "OK" and record.last_probed_at is not None


def test_probe_now_forces_a_new_complete_cycle(state_directory):
    _, agent, telegram, commands = setup(state_directory)

    async def run():
        await commands.handle_update(message("Status"))
        await commands.handle_update(message("Probe Now"))

    asyncio.run(run())
    assert agent.status_calls == 2
    assert agent.probed == list(agent.observations) * 2
    assert any("Refreshing model-status" in text for text, _ in telegram.sent)
    assert telegram.sent[-1][0] == (
        "gpt-6.1-sol ✅\ngpt-6.2-sol ❌\nclaude-opus-5-5 ❌"
    )


def test_balance_command_reads_account_without_probing_models(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("/balance")))
    assert agent.account_calls == 1
    assert agent.status_calls == 0
    assert agent.probed == []
    assert telegram.sent[-1][0] == (
        "AgentRouter balance\n\n"
        "Current balance: $12.50\n"
        "Total consumed: $5.00\n"
        "Requests: 321\n"
        "Group: vip"
    )


def test_usage_alias_reports_account_errors_without_stopping_commands(state_directory):
    class FailingAgent(FakeAgent):
        async def fetch_account_usage(self):
            self.account_calls += 1
            raise ProtocolError("invalid account response")

    settings = make_settings(state_directory)
    agent, telegram = FailingAgent(), FakeTelegram()
    commands = TelegramCommands(Prober(settings, agent, telegram))
    asyncio.run(commands.handle_update(message("/usage")))
    assert agent.account_calls == 1
    assert telegram.sent[-1][0] == "AgentRouter balance check failed: invalid account response"


def test_credit_alert_menu_confirms_spending_and_low_balance_targets(state_directory):
    settings, agent, telegram, commands = setup(state_directory)

    async def run():
        await commands.handle_update(message("/credit_alerts"))
        assert telegram.sent[-1][1]["keyboard"][0] == [
            {"text": "Spending Alert"}, {"text": "Low Balance Alert"}
        ]
        await commands.handle_update(message("Spending Alert"))
        await commands.handle_update(message("2.50", user_id=99))
        await commands.handle_update(message("0"))
        assert telegram.sent[-1][0] == "Enter a positive amount."
        assert agent.account_calls == 0
        await commands.handle_update(message("$2.50"))
        assert telegram.sent[-1][0] == (
            "Spending alert set for $2.50 more.\n"
            "You have used $5.00 so far.\n"
            "You’ll be alerted at $7.50 in total usage."
        )

        await commands.handle_update(message("Credit Alerts"))
        await commands.handle_update(message("Low Balance Alert"))
        await commands.handle_update(message("10"))
        assert telegram.sent[-1][0] == (
            "Low-balance alert set for balances below $10.00.\n"
            "Your balance is $12.50 and you have used $5.00 so far.\n"
            "At your current balance, expect the alert at about $7.50 in total usage ($2.50 more)."
        )

    asyncio.run(run())
    alerts = StateStore(settings.state_path).load().credit_alerts
    assert alerts["spending"].threshold_quota == 3_750_000
    assert alerts["balance"].threshold_quota == 5_000_000
    assert agent.account_calls == 2 and agent.status_calls == 0


def test_low_balance_projection_uses_whole_quota_units(state_directory):
    class QuotaAgent(FakeAgent):
        async def fetch_account_usage(self):
            return AccountUsage(5000, 1000, 0, "", 500_000.0, False)

    settings = make_settings(state_directory)
    telegram = FakeTelegram()
    commands = TelegramCommands(Prober(settings, QuotaAgent(), telegram))

    async def run():
        await commands.handle_update(message("Low Balance Alert"))
        await commands.handle_update(message("$1.50"))
        assert telegram.sent[-1][0] == "This account uses quota units. Enter a whole number."
        assert not commands.prober.state.credit_alerts
        await commands.handle_update(message("1,500"))

    asyncio.run(run())
    assert commands.prober.state.credit_alerts["balance"].threshold_quota == 1500
    assert "4,501 quota in total usage (3,501 quota more)" in telegram.sent[-1][0]


def test_removed_codex_controls_are_ignored(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    updates = [
        message("/set_model"),
        message("/models"),
        message("Set Codex Model"),
        {
            "update_id": 2,
            "callback_query": {
                "id": "query-1",
                "data": "codex:gpt-6.2-sol",
                "from": {"id": 42, "is_bot": False},
                "message": {"chat": {"id": 42, "type": "private"}},
            },
        },
    ]

    async def run():
        for update in updates:
            await commands.handle_update(update)

    asyncio.run(run())
    assert agent.status_calls == 0
    assert telegram.sent == []


def test_other_users_cannot_probe_or_change_schedule(state_directory):
    _, agent, telegram, commands = setup(state_directory)

    async def run():
        await commands.handle_update(message("Probe Now", user_id=99))
        await commands.handle_update(message("Every 5 min", user_id=99))

    asyncio.run(run())
    assert agent.status_calls == 0
    assert commands.prober.poll_seconds == 900
    assert telegram.sent == []


def test_schedule_describes_exact_dynamic_policy(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Probing Schedule")))
    text, markup = telegram.sent[-1]
    assert "Every 15 minutes" in text
    assert "every model listed there" in text
    assert "At most 2 model probes run at once" in text
    assert [[button["text"] for button in row] for row in markup["keyboard"]] == [
        ["Every 5 min", "Every 15 min", "Every 30 min"],
        ["Use default", "Back"],
    ]
    assert agent.status_calls == 0


def test_schedule_choice_survives_restart_and_can_be_reset(state_directory):
    settings, _, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Every 30 min")))

    assert commands.prober.poll_seconds == 1800
    assert telegram.sent[-1][0] == "Automatic probes every 30 minutes."
    assert telegram.sent[-1][1]["keyboard"][1][-1]["text"] == "Probing Schedule"

    agent, telegram = FakeAgent(), FakeTelegram()
    restarted_settings = replace(settings, poll_seconds=1200)
    restarted = TelegramCommands(Prober(restarted_settings, agent, telegram))

    async def reset():
        await restarted.handle_update(message("/schedule"))
        assert "Every 30 minutes" in telegram.sent[-1][0]
        await restarted.handle_update(message("Use default"))

    asyncio.run(reset())
    assert restarted.prober.poll_seconds == 1200
    assert telegram.sent[-1][0] == "Automatic probes every 20 minutes."
    assert StateStore(settings.state_path).load().poll_seconds_override is None
    assert agent.status_calls == 0


def test_schedule_save_failure_keeps_the_current_interval(state_directory, monkeypatch):
    _, _, telegram, commands = setup(state_directory)

    def fail_to_save(state):
        raise OSError("Unable to write state")

    monkeypatch.setattr(commands.prober.store, "save", fail_to_save)
    asyncio.run(commands.handle_update(message("Every 30 min")))

    assert commands.prober.poll_seconds == 900
    assert telegram.sent[-1][0] == "The probing interval could not be saved. Try again."
