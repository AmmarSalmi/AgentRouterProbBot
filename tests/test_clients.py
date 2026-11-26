import asyncio
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
        agentrouter_dashboard_token="dashboard-token",
        agentrouter_user_id=60923,
        client_user_agent="codex_cli_rs/test",
    )


def test_status_uses_dashboard_token_and_parses_only_returned_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers
        assert request.headers["New-Api-User"] == "60923"
        assert request.headers["Authorization"] == "Bearer dashboard-token"
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
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), cookies={"session": "stale"}
        ) as http:
            models = await AgentRouterClient(settings(), http).fetch_model_status()
            assert list(models) == ["gpt-6.2-sol", "claude-opus-5-5"]
            assert models["gpt-6.2-sol"].apis == ("chat", "responses")
            assert models["claude-opus-5-5"].apis == ("claude_native",)
            assert models["gpt-6.2-sol"].success_rate_24h == 99.5

    asyncio.run(run())


def test_account_usage_reads_balance_and_consumption():
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
        assert request.headers["Authorization"] == "Bearer dashboard-token"
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            headers={"Set-Cookie": "session=server-issued; Path=/"},
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
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            usage = await AgentRouterClient(settings(), http).fetch_account_usage()
            assert usage == AccountUsage(6250000, 2500000, 321, "vip", 500000.0, True)
            assert paths == ["/api/user/self", "/api/status"]

    asyncio.run(run())


@pytest.mark.parametrize("unlimited", [False, True])
def test_probe_key_balance_matches_the_configured_key_across_dashboard_pages(unlimited):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/api/token/"
        assert request.headers["Authorization"] == "Bearer dashboard-token"
        assert request.headers["New-Api-User"] == "60923"
        assert "cookie" not in request.headers
        page = int(request.url.params["p"])
        records = {
            1: {"key": "different-test", "remain_quota": 0, "unlimited_quota": False},
            2: {"key": "test", "remain_quota": 450_000, "unlimited_quota": unlimited},
        }
        row = {"id": page, "name": "Coding", "status": 1, "used_quota": 1000, **records[page]}
        return httpx.Response(200, json={
            "success": True,
            "data": {"items": [row], "page": page, "page_size": 1, "total": 2},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await AgentRouterClient(settings(), http).fetch_api_keys()

    keys = asyncio.run(run())
    assert set(keys) == {1, 2}
    assert not keys[1].is_probe_key and keys[2].is_probe_key
    assert keys[2].remaining_quota == (None if unlimited else 450_000)
    assert keys[2].used_quota == 1000
    assert "different-test" not in repr(keys)
    assert paths == ["/api/token/"] * 2


def test_quota_display_retries_fallback_then_caches_valid_settings():
    replies = iter((
        httpx.Response(503),
        httpx.Response(200, json={"data": {"quota_per_unit": 750_000, "display_in_currency": False}}),
    ))

    def handler(request):
        assert request.url.path == "/api/status"
        return next(replies)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AgentRouterClient(settings(), http)
            assert await client.quota_display_settings() == (500_000, True)
            assert await client.quota_display_settings() == (750_000, False)
            assert await client.quota_display_settings() == (750_000, False)

    asyncio.run(run())


def test_usage_totals_group_by_numeric_key_across_pages_and_respect_end_boundary():
    rows = [
        {"token_id": 2, "token_name": "Current", "model_name": "alpha", "created_at": 199, "quota": 6},
        {"token_id": 4, "token_name": "Current", "model_name": "beta", "created_at": 180, "quota": 3},
        {"token_id": 2, "token_name": "Previous", "model_name": "beta", "created_at": 100, "quota": 4},
        {"token_id": 2, "token_name": "Current", "model_name": "next", "created_at": 200, "quota": 90},
    ]

    def handler(request):
        assert request.url.path == "/api/log/self"
        assert request.headers["Authorization"] == "Bearer dashboard-token"
        assert request.headers["New-Api-User"] == "60923"
        assert request.url.params["type"] == "2"
        assert request.url.params["start_timestamp"] == "100"
        assert request.url.params["end_timestamp"] == "199"
        assert "token_name" not in request.url.params
        page = int(request.url.params["p"])
        return httpx.Response(200, json={"success": True, "data": {
            "items": rows[(page - 1) * 2:page * 2], "page": page, "page_size": 2, "total": 4,
        }})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AgentRouterClient(settings(), http)
            key_totals = await client.fetch_usage_totals(100, 200, key_id=2)
            account_totals = await client.fetch_usage_totals(100, 200)
            assert (key_totals.spent, key_totals.requests) == (10, 2)
            assert key_totals.by_model == {"alpha": 6, "beta": 4}
            assert account_totals.by_key == {2: 10, 4: 3}
            assert account_totals.key_names == {2: "Current", 4: "Current"}

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
def test_dashboard_auth_failures_give_credential_guidance(response, expected_message):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as http:
            with pytest.raises(AuthenticationError, match=expected_message):
                await AgentRouterClient(settings(), http).fetch_model_status()

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
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await AgentRouterClient(settings(), http).probe_model(observation)
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
                await AgentRouterClient(settings(), http).probe_model(
                    ModelObservation("gpt", "unknown", "none", ("responses",))
                )

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
