import asyncio
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
    config = directory / "config.toml"
    config.write_text(
        'model = "gpt-6.1-sol"\n'
        'model_provider = "AgentRouter"\n\n'
        '[model_providers.AgentRouter]\n'
        'base_url = "https://agentrouter.org/v1"\n'
        'env_key = "CODEX_GATEWAY_API_KEY"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
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
        codex_config_path=config,
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


def callback(model, user_id=42):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "query-1",
            "data": f"codex:{model}",
            "from": {"id": user_id, "is_bot": False},
            "message": {"chat": {"id": 42, "type": "private"}},
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
        self.answered = []

    async def send(self, text, *, reply_markup=None):
        self.sent.append((text, reply_markup))

    async def answer_callback_query(self, query_id, text=None):
        self.answered.append((query_id, text))


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


def test_codex_picker_only_shows_responses_models(state_directory):
    _, _, telegram, commands = setup(state_directory)

    async def run():
        await commands.prober.run_cycle()
        await commands.handle_update(message("Set Codex Model"))

    asyncio.run(run())
    text, markup = telegram.sent[-1]
    labels = [row[0]["text"] for row in markup["inline_keyboard"]]
    assert labels == ["✓ gpt-6.1-sol", "gpt-6.2-sol"]
    assert "claude-opus-5-5" in text and "Not Codex-compatible" in text


def test_codex_callback_updates_toml_and_answers_query(state_directory):
    settings, _, telegram, commands = setup(state_directory)

    async def run():
        await commands.prober.run_cycle()
        await commands.handle_update(callback("gpt-6.2-sol"))

    asyncio.run(run())
    assert telegram.answered == [("query-1", "Updating Codex…")]
    text = settings.codex_config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-6.2-sol"' in text
    assert settings.codex_config_path.with_name(
        "config.toml.agentrouter-prober.bak"
    ).is_file()
    assert "now gpt-6.2-sol" in telegram.sent[-1][0]


def test_other_users_cannot_probe_or_change_codex(state_directory):
    settings, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Probe Now", user_id=99)))
    asyncio.run(commands.handle_update(callback("gpt-6.2-sol", user_id=99)))
    assert agent.status_calls == 0
    assert telegram.sent == [] and telegram.answered == []
    assert 'model = "gpt-6.1-sol"' in settings.codex_config_path.read_text(
        encoding="utf-8"
    )


def test_schedule_describes_exact_dynamic_policy(state_directory):
    _, agent, telegram, commands = setup(state_directory)
    asyncio.run(commands.handle_update(message("Probing Schedule")))
    text = telegram.sent[-1][0]
    assert "Every 15 minutes" in text
    assert "every model listed there" in text
    assert "documentation or pricing page are never added" in text
    assert agent.status_calls == 0
