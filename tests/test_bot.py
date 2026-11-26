import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path

from agentrouter_prober.app import Prober
from agentrouter_prober.bot import TelegramCommands
from agentrouter_prober.clients import (
    AccountUsage,
    ApiKey,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    ProtocolError,
    TelegramBot,
    UsageTotals,
)
from agentrouter_prober.config import Settings, TelegramSettings
from agentrouter_prober.key_messages import DAY_SECONDS, key_summary
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


def message(text, user_id=42, chat_id=42, chat_type="private", is_bot=False, **message_fields):
    return {
        "update_id": 1,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot},
            **message_fields,
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

    async def fetch_api_keys(self):
        return {1: ApiKey(1, "Prober", 1, None, 0, is_probe_key=True)}

    async def quota_display_settings(self):
        return 500_000.0, True

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
        telegram, Prober(settings, agent, telegram)
    )


def test_status_discovers_and_probes_all_models_once(state_directory):
    settings, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("📡 Status")))
    assert agent.status_calls == 1
    assert agent.probed == list(agent.observations)
    assert telegram.sent[-1][0] == (
        "📡 Model Status\n\n✅ gpt-6.1-sol\n❌ gpt-6.2-sol\n❌ claude-opus-5-5"
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
        await commands.handle_update(message("⚡ Probe Now"))

    asyncio.run(run())
    assert agent.status_calls == 2
    assert agent.probed == list(agent.observations) * 2
    assert any("⏳ Probing every listed model…" == text for text, _ in telegram.sent)
    assert telegram.sent[-1][0] == (
        "📡 Model Status\n\n✅ gpt-6.1-sol\n❌ gpt-6.2-sol\n❌ claude-opus-5-5"
    )


def test_balance_command_reads_account_without_probing_models(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("/balance")))
    assert agent.account_calls == 1
    assert agent.status_calls == 0
    assert agent.probed == []
    assert telegram.sent[-1][0] == (
        "💰 $12.50 available\n\n"
        "Total spent · $5.00\n"
        "Requests · 321\n"
        "Group · vip"
    )


def test_usage_alias_reports_account_errors_without_stopping_commands(state_directory):
    class FailingAgent(FakeAgent):
        async def fetch_account_usage(self):
            self.account_calls += 1
            raise ProtocolError("invalid account response")

    settings = make_settings(state_directory)
    agent, telegram = FailingAgent(), FakeTelegram()
    commands = TelegramCommands(telegram, Prober(settings, agent, telegram))
    asyncio.run(commands.handle_update(message("/usage")))
    assert agent.account_calls == 1
    assert telegram.sent[-1][0] == "AgentRouter balance check failed: invalid account response"


def test_credit_alert_menu_confirms_spending_and_low_balance_targets(state_directory):
    settings, agent, telegram, commands = setup(state_directory)

    async def run():
        await commands.handle_update(message("🔔 Credit Alerts"))
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
            "🔔 Spending alert set\n\n"
            "$5.00 spent · $2.50 more\n"
            "Alert at $7.50 total spent"
        )

        await commands.handle_update(message("Credit Alerts"))
        await commands.handle_update(message("Low Balance Alert"))
        await commands.handle_update(message("10"))
        assert telegram.sent[-1][0] == (
            "🔔 Low balance alert set\n\n"
            "$12.50 left · $5.00 spent\n"
            "Alert when balance is below $10.00\n"
            "Est. spend at alert · $7.50 ($2.50 more)"
        )

    asyncio.run(run())
    alerts = StateStore(settings.state_path).load().credit_alerts
    assert alerts["spending"].threshold_quota == 3_750_000
    assert alerts["balance"].threshold_quota == 5_000_000
    assert agent.account_calls == 2 and agent.status_calls == 0


def test_api_key_menu_history_alerts_and_troubleshooting_are_private(state_directory):
    settings = make_settings(state_directory)

    class KeyAgent(FakeAgent):
        async def fetch_api_keys(self):
            return {
                4: ApiKey(4, "Coding", 4, 0, 2_500_000, model_limits=("allowed",), allowed_ips=("203.0.113.10",)),
                9: ApiKey(9, "Coding", 1, None, 8_000_000),
                15: ApiKey(15, "Other", 2, 2_000_000, 3_000_000),
            }

        async def fetch_usage_totals(self, start, end, *, key_id=None):
            assert key_id == 4
            self.periods.append(end - start)
            return UsageTotals(1_250_000, 3, by_model=Counter({"allowed": 1_250_000}))

    agent = KeyAgent()
    agent.periods = []
    telegram = FakeTelegram()
    prober = Prober(settings, agent, telegram)
    prober.state.active_models = ["allowed", "other"]
    prober.state.model("allowed").availability = "available"
    commands = TelegramCommands(telegram, prober)

    async def run():
        await commands.handle_update(message("/api_keys", user_id=70))
        await commands.handle_update(message("/api_keys", chat_type="group"))
        assert not telegram.sent
        await commands.handle_update(message("🔑 API Keys"))
        text, keyboard = telegram.sent[-1]
        assert text == (
            "🔑 API Keys\n💰 Shared balance · $12.50\nBars show % spent · [spent/left]\n\n"
            "❌ Coding (#4) · Out of quota\n██████████ [$5.00/$0.00]\n\n"
            "✅ Coding (#9)\n[$16.00/∞] · Unlimited\n\n"
            "❌ Other (#15) · Disabled\n██████░░░░ [$6.00/$4.00]"
        )
        first_label = keyboard["keyboard"][0][0]["text"]
        assert first_label == "🔑 Coding (#4)"
        await commands.handle_update(message(first_label, user_id=70))
        assert telegram.sent[-1][0] == text
        await commands.handle_update(message(first_label))
        assert "Last use ·" in telegram.sent[-1][0] and "Expires ·" in telegram.sent[-1][0]
        for label in ("Last 24 Hours", "Last 7 Days"):
            await commands.handle_update(message(label))
            assert "$2.50 spent · 3 logged requests" in telegram.sent[-1][0]
            assert "allowed · $2.50" in telegram.sent[-1][0]
        await commands.handle_update(message("🛠️ Troubleshoot Key"))
        assert "Allowed IPs · 203.0.113.10" in telegram.sent[-1][0]
        assert "✅ allowed" in telegram.sent[-1][0]
        assert "❔ other · 🔒 Restricted" in telegram.sent[-1][0]
        await commands.handle_update(message("🔔 Key Alerts"))
        await commands.handle_update(message("Spend Alert"))
        await commands.handle_update(message("$2.50"))
        assert "alert at $7.50 total" in telegram.sent[-1][0]
        await commands.handle_update(message("Key Low-Quota Alert"))
        await commands.handle_update(message("1"))
        assert "already due" in telegram.sent[-1][0]

    asyncio.run(run())
    assert agent.periods == [DAY_SECONDS, 7 * DAY_SECONDS]
    assert not agent.probed
    saved = StateStore(settings.state_path).load().key_monitoring
    assert set(saved.key_alerts) == {(4, "spending"), (4, "balance")}


def test_quota_bar_reserves_a_segment_until_quota_runs_out():
    display = (500_000.0, False)
    key = ApiKey(1, "Coding", 1, 10_000, 0)
    assert key_summary(key, display, 0) == "✅ Coding (#1)\n░░░░░░░░░░ [0 quota/10,000 quota]"
    near_limit = replace(key, remaining_quota=1, used_quota=9_999)
    assert key_summary(near_limit, display, 0) == "✅ Coding (#1)\n█████████░ [9,999 quota/1 quota]"
    empty_limit = replace(key, remaining_quota=0)
    assert key_summary(empty_limit, display, 0) == "❌ Coding (#1) · Out of quota\n░░░░░░░░░░ [0 quota/0 quota]"


def test_api_key_page_buttons_and_truncated_names_round_trip(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    long_name = "Key #1 · " + "long " * 16 + "name (#3)"

    async def keys():
        entries = {id_: ApiKey(id_, f"Code {id_}", 1, 2_000_000, 1_000_000) for id_ in range(1, 9)}
        return {**entries, 9: ApiKey(9, long_name, 1, 2_000_000, 1_000_000)}

    agent.fetch_api_keys = keys

    async def run():
        await commands.handle_update(message("/api_keys"))
        assert telegram.sent[-1][0].startswith("🔑 API Keys · 1/2\n")
        await commands.handle_update(message("➡️ Next Keys"))
        text, keyboard = telegram.sent[-1]
        assert text.startswith("🔑 API Keys · 2/2\n")
        assert f"✅ {long_name} (#9)" in text
        label = keyboard["keyboard"][0][0]["text"]
        assert len(label) <= 64 and label.endswith("… (#9)")
        await commands.handle_update(message(label))
        assert telegram.sent[-1][0].startswith(f"✅ {long_name} (#9)\n")
        await commands.handle_update(message("Key #9 · Previous key name (#3)"))
        assert telegram.sent[-1][0].startswith(f"✅ {long_name} (#9)\n")
        await commands.handle_update(message("⬅️ Previous Keys"))
        assert telegram.sent[-1][0].startswith("🔑 API Keys · 1/2\n")
        await commands.handle_update(message("↩️ Back"))
        assert telegram.sent[-1][0] == "🤖 AgentRouter"

    asyncio.run(run())


def test_weekly_digest_menu_views_report_and_persists_subscription(state_directory):
    settings, _, telegram, commands = setup(state_directory)

    async def totals(start, end):
        return UsageTotals(100_000, 1)

    commands.prober.agentrouter.fetch_usage_totals = totals

    async def run():
        await commands.handle_update(message("/weekly_digest"))
        assert "Auto-send · Off" in telegram.sent[-1][0]
        await commands.handle_update(message("View Report"))
        assert "$0.20 spent · 1 logged request\n→ Same as prior week ($0.20)" in telegram.sent[-1][0]
        await commands.handle_update(message("Enable Auto-send"))
        assert "Auto-send · On" in telegram.sent[-1][0]
        assert StateStore(settings.state_path).load().key_monitoring.weekly_digest is not None
        await commands.handle_update(message("Disable Weekly Digest"))
        assert StateStore(settings.state_path).load().key_monitoring.weekly_digest is None

    asyncio.run(run())


def test_low_balance_projection_uses_whole_quota_units(state_directory):
    class QuotaAgent(FakeAgent):
        async def fetch_account_usage(self):
            return AccountUsage(5000, 1000, 0, "", 500_000.0, False)

    settings = make_settings(state_directory)
    telegram = FakeTelegram()
    commands = TelegramCommands(telegram, Prober(settings, QuotaAgent(), telegram))

    async def run():
        await commands.handle_update(message("Low Balance Alert"))
        await commands.handle_update(message("$1.50"))
        assert telegram.sent[-1][0] == "This account uses quota units. Enter a whole number."
        assert not commands.prober.state.credit_alerts
        await commands.handle_update(message("1,500"))

    asyncio.run(run())
    assert commands.prober.state.credit_alerts["balance"].threshold_quota == 1500
    assert "Est. spend at alert · 4,501 quota (3,501 quota more)" in telegram.sent[-1][0]


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


def test_commands_ignore_attachments_and_accept_text_replies(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    photo = {"photo": [{"file_id": "photo-id"}]}

    async def run():
        await commands.handle_update(message(None, caption="/probe", **photo))
        await commands.handle_update(message("/probe", document={"file_id": "document-id"}))
        assert telegram.sent == []
        await commands.handle_update(message("/balance", reply_to_message=photo))

    asyncio.run(run())
    assert agent.status_calls == 0
    assert agent.account_calls == 1


def test_schedule_describes_exact_dynamic_policy(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("⏱️ Schedule")))
    text, markup = telegram.sent[-1]
    assert "Every 15 minutes" in text
    assert "One probe per model on the dashboard" in text
    assert "up to 2 probes at once" in text
    assert [[button["text"] for button in row] for row in markup["keyboard"]] == [
        ["Every 5 min", "Every 15 min", "Every 30 min"],
        ["Use default", "↩️ Back"],
    ]
    assert agent.status_calls == 0


def test_schedule_choice_survives_restart_and_can_be_reset(state_directory):
    settings, _, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Every 30 min")))

    assert commands.prober.poll_seconds == 1800
    assert telegram.sent[-1][0] == "✅ Automatic probes every 30 minutes"
    assert telegram.sent[-1][1]["keyboard"][3][0]["text"] == "⏱️ Schedule"

    agent, telegram = FakeAgent(), FakeTelegram()
    restarted_settings = replace(settings, poll_seconds=1200)
    restarted = TelegramCommands(telegram, Prober(restarted_settings, agent, telegram))

    async def reset():
        await restarted.handle_update(message("/schedule"))
        assert "Every 30 minutes" in telegram.sent[-1][0]
        await restarted.handle_update(message("Use default"))

    asyncio.run(reset())
    assert restarted.prober.poll_seconds == 1200
    assert telegram.sent[-1][0] == "✅ Automatic probes every 20 minutes"
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
