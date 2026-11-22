import tomllib

import pytest

from agentrouter_prober.codex import CodexConfigError, CodexConfigManager


def test_model_update_preserves_provider_and_creates_backup(state_directory):
    path = state_directory / "config.toml"
    original = (
        'model = "old-model"\n'
        'model_provider = "openai"\n'
        'model_reasoning_effort = "low"\n\n'
        '[model_providers.AgentRouter]\n'
        'base_url = "https://agentrouter.org/v1"\n'
        'env_key = "CODEX_GATEWAY_API_KEY"\n'
        'wire_api = "responses"\n\n'
        '[windows]\n'
        'sandbox = "unelevated"\n'
    )
    path.write_text(original, encoding="utf-8")
    manager = CodexConfigManager(path)
    result = manager.set_model("gpt-6.2-sol")
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert result.model == "gpt-6.2-sol"
    assert parsed["model"] == "gpt-6.2-sol"
    assert parsed["model_provider"] == "AgentRouter"
    assert parsed["windows"]["sandbox"] == "unelevated"
    assert path.with_name("config.toml.agentrouter-prober.bak").read_text(
        encoding="utf-8"
    ) == original


def test_update_refuses_config_without_agentrouter_provider(state_directory):
    path = state_directory / "config.toml"
    path.write_text('model = "gpt"\n', encoding="utf-8")
    with pytest.raises(CodexConfigError, match=r"no \[model_providers\.AgentRouter\]"):
        CodexConfigManager(path).set_model("new")
