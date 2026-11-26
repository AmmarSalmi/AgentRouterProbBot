import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from agentrouter_prober.clients import (
    AccountUsage,
    AgentRouterClient,
    AuthenticationError,
    ModelObservation,
    ProbeOutcome,
    ProtocolError,
    TelegramClient,
    TransientClientError,
)
from agentrouter_prober.config import Settings, TelegramSettings


def settings() -> Settings:
    return Settings(
        agentrouter_base_url="https://agentrouter.test",
        agentrouter_api_key="sk-test",
        telegram=TelegramSettings("bot", "42", "https://telegram.test"),
        poll_seconds=900,
        probe_max_output_tokens=32,
        probe_concurrency=2,
        request_timeout_seconds=90,
        state_path=Path("state.json"),
        health_max_age_seconds=3600,
        zen_profile_path=None,
        agentrouter_session_cookie="browser-session",
        agentrouter_user_id=60923,
        codex_config_path=Path("config.toml"),
        client_user_agent="codex_cli_rs/test",
    )


def test_status_uses_browser_session_and_parses_only_returned_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cookie"] == "session=browser-session"
        assert request.headers["New-Api-User"] == "60923"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "models": [
                        {
                            "name": "gpt-6.2-sol",
                            "status": "operational",
                            "current_tier": "ok",
                            "apis": ["chat", "responses"],
                            "success_rate_24h": 99.5,
                            "ttft_p90_ms_24h": 9000,
                            "avg_latency_ms": 4500,
                        },
                        {
                            "name": "claude-opus-5-5",
                            "status": "no_traffic",
                            "current_tier": "none",
                            "apis": ["claude_native"],
                        },
                    ]
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            models = await AgentRouterClient(settings(), http).fetch_model_status()
            assert list(models) == ["gpt-6.2-sol", "claude-opus-5-5"]
            assert models["gpt-6.2-sol"].codex_compatible
            assert not models["claude-opus-5-5"].codex_compatible
            assert models["gpt-6.2-sol"].success_rate_24h == 99.5

    asyncio.run(run())


def test_status_auth_failure_inside_http_200_is_recognized():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"success": False, "message": "New-Api-User mismatch"},
                )
            )
        ) as http:
            with pytest.raises(AuthenticationError):
                await AgentRouterClient(settings(), http).fetch_model_status()

    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "dashboard-token"])
def test_account_usage_reads_balance_and_consumption(token):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/status":
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            assert "new-api-user" not in request.headers
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "quota_per_unit": 500000,
                        "display_in_currency": True,
                    },
                },
            )
        assert request.url.path == "/api/user/self"
        assert request.headers["New-Api-User"] == "60923"
        if token is None:
            assert request.headers["Cookie"] == "session=browser-session"
            assert "authorization" not in request.headers
        else:
            assert request.headers["Authorization"] == f"Bearer {token}"
            assert "cookie" not in request.headers
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "quota": 6250000,
                    "used_quota": 2500000,
                    "request_count": 321,
                    "group": "vip",
                },
            },
        )

    async def run():
        config = replace(settings(), agentrouter_dashboard_token=token)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            usage = await AgentRouterClient(config, http).fetch_account_usage()
            assert usage == AccountUsage(6250000, 2500000, 321, "vip", 500000.0, True)
            assert paths == ["/api/user/self", "/api/status"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "response,expected_message",
    [
        (httpx.Response(401), "AGENTROUTER_DASHBOARD_TOKEN and AGENTROUTER_USER_ID"),
        (httpx.Response(403), "AGENTROUTER_DASHBOARD_TOKEN and AGENTROUTER_USER_ID"),
        (
            httpx.Response(200, json={"success": False, "message": "New-Api-User mismatch"}),
            "AGENTROUTER_DASHBOARD_TOKEN and AGENTROUTER_USER_ID",
        ),
        (
            httpx.Response(200, text="<html>Verify your browser</html>"),
            "check AgentRouter access from this host",
        ),
    ],
)
def test_dashboard_token_auth_failures_give_token_mode_guidance(response, expected_message):
    async def run():
        config = replace(settings(), agentrouter_dashboard_token="dashboard-token")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as http:
            with pytest.raises(AuthenticationError, match=expected_message):
                await AgentRouterClient(config, http).fetch_model_status()

    asyncio.run(run())


@pytest.mark.parametrize(
    "observation,path,auth_header,response",
    [
        (
            ModelObservation("gpt", "operational", "ok", ("responses",)),
            "/v1/responses",
            "authorization",
            {
                "id": "resp-test",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            },
        ),
        (
            ModelObservation("chat", "operational", "ok", ("chat",)),
            "/v1/chat/completions",
            "authorization",
            {"choices": [{"message": {"content": "OK"}}]},
        ),
        (
            ModelObservation("opus", "no_traffic", "none", ("claude_native",)),
            "/v1/messages",
            "x-api-key",
            {
                "id": "msg-test",
                "type": "message",
                "content": [{"type": "text", "text": "OK"}],
            },
        ),
    ],
)
def test_probe_selects_api_advertised_by_model_status(
    observation, path, auth_header, response
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        expected_key = "Bearer sk-test" if auth_header == "authorization" else "sk-test"
        assert request.headers[auth_header] == expected_key
        assert "cookie" not in request.headers
        assert "new-api-user" not in request.headers
        return httpx.Response(200, json=response)

    async def run():
        config = replace(settings(), agentrouter_dashboard_token="dashboard-token")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await AgentRouterClient(config, http).probe_model(observation)
            assert result.outcome is ProbeOutcome.AVAILABLE
            assert result.reply == "OK"

    asyncio.run(run())


@pytest.mark.parametrize("status", [402, 503])
def test_probe_classifies_exhausted_or_unrouted_model_as_unavailable(status):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status, json={"error": {"message": "no available channel"}}
                )
            )
        ) as http:
            result = await AgentRouterClient(settings(), http).probe_model(
                ModelObservation("gpt", "no_traffic", "none", ("responses",))
            )
            assert result.outcome is ProbeOutcome.UNAVAILABLE

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        "<html>Maintenance</html>",
        '{"success":false,"message":"Service unavailable"}',
        "[]",
    ],
)
def test_probe_rejects_http_success_without_model_output(body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        ) as http:
            with pytest.raises(ProtocolError):
                await AgentRouterClient(settings(), http).confirm_model("gpt")

    asyncio.run(run())


@pytest.mark.parametrize("parameters,expected_delay", [({"retry_after": 3600}, 3600), ({}, 30)])
def test_telegram_rate_limit_always_has_retry_delay(parameters, expected_delay):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, json={"ok": False, "parameters": parameters}
                )
            )
        ) as http:
            client = TelegramClient(settings().telegram, http_client=http)
            with pytest.raises(TransientClientError) as caught:
                await client.send("Alert")
            assert caught.value.retry_after == expected_delay

    asyncio.run(run())
