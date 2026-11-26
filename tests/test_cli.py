import asyncio
import json
import signal

import httpx
import pytest

from agentrouter_prober import cli
from agentrouter_prober.config import Settings
from agentrouter_prober.messages import TELEGRAM_HELP
from agentrouter_prober.state import StateStore, health_is_fresh
from agentrouter_prober.telegram import CONNECTED_MESSAGE, TelegramRegistrationStore


@pytest.fixture
def configured_env(monkeypatch, state_directory):
    values = {
        "TELEGRAM_BOT_TOKEN": "123:cli-test-token",
        "TELEGRAM_API_BASE_URL": "https://telegram.test",
        "TELEGRAM_CHAT_ID": "",
        "AGENTROUTER_API_KEY": "sk-test",
        "AGENTROUTER_DASHBOARD_TOKEN": "dashboard-token",
        "AGENTROUTER_USER_ID": "60923",
        "AGENTROUTER_BASE_URL": "https://agentrouter.test",
        "POLL_SECONDS": "900",
        "STATE_PATH": str(state_directory / "state.json"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return state_directory / "state.json"


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    clients = []

    def respond(request):
        if request.url.host == "agentrouter.test" and request.url.path == "/api/token/":
            assert request.headers["Authorization"] == "Bearer dashboard-token"
            assert request.headers["New-Api-User"] == "60923"
            return httpx.Response(200, json={
                "success": True,
                "data": {"items": [{"key": "test", "unlimited_quota": True}]},
            })
        return handler(request)

    def make_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return clients


def telegram_response(sent):
    acknowledged = 0

    def respond(request):
        nonlocal acknowledged
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "username": "test_bot"}
        elif method in {"setMyCommands", "setChatMenuButton"}:
            result = True
        elif method == "getUpdates":
            acknowledged = max(acknowledged, data.get("offset", 0))
            result = [] if acknowledged > 1 else [{
                "update_id": 1,
                "message": {
                    "text": "/start",
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                },
            }]
        else:
            assert method == "sendMessage"
            sent.append(data)
            result = {"message_id": len(sent)}
        return httpx.Response(200, json={"ok": True, "result": result})

    return respond


def test_telegram_only_mode_registers_without_agentrouter_credentials(configured_env, monkeypatch):
    monkeypatch.delenv("AGENTROUTER_API_KEY")
    monkeypatch.delenv("CODEX_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGENTROUTER_DASHBOARD_TOKEN")
    monkeypatch.delenv("AGENTROUTER_USER_ID")
    sent = []
    telegram = telegram_response(sent)

    def handler(request):
        assert request.url.host == "telegram.test"
        return telegram(request)

    clients = install_transport(monkeypatch, handler)
    assert cli.main(["--test-telegram"]) == 0
    assert [item["text"] for item in sent] == [
        CONNECTED_MESSAGE, "AgentRouter prober Telegram test successful."
    ]
    assert all(item["chat_id"] == "42" for item in sent)
    assert TelegramRegistrationStore(configured_env).load().chat_id == 42
    assert not configured_env.exists()
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "name,value,reason",
    [
        ("AGENTROUTER_DASHBOARD_TOKEN", " ", "is required"),
        ("AGENTROUTER_USER_ID", "", "is required"),
        ("AGENTROUTER_USER_ID", "zero", "must be an integer"),
        ("AGENTROUTER_USER_ID", "0", "must be greater than zero"),
    ],
)
def test_cli_rejects_invalid_dashboard_credentials(
    configured_env, monkeypatch, caplog, name, value, reason
):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: pytest.fail("Network initialized with invalid credentials")
    )

    assert cli.main(["--once"]) == 2
    assert f"{name} {reason}" in caplog.text
    assert not configured_env.exists()


@pytest.mark.parametrize("once", [True, False])
def test_polling_starts_after_registration_and_delivers_alerts(
    configured_env, monkeypatch, once, completed_response
):
    sent, model_requests = [], []
    telegram = telegram_response(sent)

    def handler(request):
        if request.url.host == "telegram.test":
            return telegram(request)
        registration = TelegramRegistrationStore(configured_env).load()
        assert registration.chat_id == 42 and registration.next_update_id is None
        assert sent[0]["text"] == CONNECTED_MESSAGE
        model_requests.append(request.url.path)
        if request.url.path == "/api/user/model-status":
            assert request.headers["new-api-user"] == "60923"
            assert request.headers["authorization"] == "Bearer dashboard-token"
            assert "cookie" not in request.headers
            return httpx.Response(200, json={"success": True, "data": {"models": [{
                "name": "gpt-6-astra", "status": "operational", "current_tier": "ok",
                "apis": ["responses"],
            }]}})
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert "cookie" not in request.headers
        assert "new-api-user" not in request.headers
        return httpx.Response(200, json=completed_response)

    clients = install_transport(monkeypatch, handler)
    if not once:
        async def one_cycle(self, stop_event):
            await self.run_cycle()
            stop_event.set()

        monkeypatch.setattr(cli.Prober, "run_forever", one_cycle)
    assert not health_is_fresh(configured_env, 3600)
    assert cli.main(["--once"] if once else []) == 0
    assert model_requests == ["/api/user/model-status", "/v1/responses"]
    assert len(sent) == 2 and all(item["chat_id"] == "42" for item in sent)
    assert health_is_fresh(configured_env, 3600)
    assert not StateStore(configured_env).load().model("gpt-6-astra").notification_pending
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("mode", [["--test-telegram"], ["--once"], []])
def test_shutdown_interrupts_setup_and_closes_clients(configured_env, monkeypatch, mode):
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    telegram = telegram_response([])

    async def handler(request):
        assert request.url.host == "telegram.test"
        if request.url.path.endswith("/getMe"):
            return telegram(request)
        assert request.url.path.endswith("/getUpdates")
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        await asyncio.Event().wait()

    clients = install_transport(monkeypatch, handler)
    assert cli.main(mode) == 0
    assert not TelegramRegistrationStore(configured_env).path.exists()
    assert not configured_env.exists()
    assert clients and all(client.is_closed for client in clients)
    assert {signum: signal.getsignal(signum) for signum in previous} == previous


def test_invalid_token_reports_error_and_closes_client(configured_env, monkeypatch, caplog):
    clients = install_transport(
        monkeypatch, lambda request: httpx.Response(401, json={"ok": False})
    )
    assert cli.main(["--test-telegram"]) == 2
    assert "Telegram bot token or chat access was rejected" in caplog.text
    assert "cli-test-token" not in caplog.text
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("stage,once", [
    ("test_send", True),
    ("dashboard", True),
    ("dashboard", False),
    ("confirmation", True),
    ("confirmation", False),
    ("alert_send", True),
    ("alert_send", False),
])
def test_shutdown_cancels_active_requests_and_preserves_progress(
    configured_env, monkeypatch, completed_response, stage, once, signum
):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    stop_sent = False
    cancelled = []
    requested = []
    sent = []
    telegram = telegram_response(sent)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    async def handler(request):
        nonlocal stop_sent
        path = request.url.path
        requested.append(path)
        target = (
            (stage in {"test_send", "alert_send"} and path.endswith("/sendMessage"))
            or (stage == "dashboard" and path == "/api/user/model-status")
            or (stage == "confirmation" and path == "/v1/responses")
        )
        if target and not stop_sent:
            stop_sent = True
            signal.getsignal(signum)(signum, None)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(path)
                raise
        if request.url.host == "telegram.test":
            if path.endswith("/getUpdates"):
                await asyncio.Event().wait()  # This preconfigured chat has no queued commands.
            return telegram(request)
        if path == "/api/user/model-status":
            return httpx.Response(200, json={"success": True, "data": {"models": [{
                "name": "gpt-6-astra", "status": "operational", "current_tier": "ok",
                "apis": ["responses"],
            }]}})
        assert path == "/v1/responses"
        return httpx.Response(200, json=completed_response)

    clients = install_transport(monkeypatch, handler)

    async def run():
        operation = (
            cli._test_telegram() if stage == "test_send"
            else cli._run(Settings.from_env(), once)
        )
        assert await asyncio.wait_for(operation, timeout=1) == 0
        assert len(cancelled) == 1
        assert not sent
        assert all(client.is_closed for client in clients)
        if stage == "alert_send":
            saved = StateStore(configured_env).load().model("gpt-6-astra")
            assert saved.availability == "available" and saved.notification_pending
            assert await asyncio.wait_for(cli._run(Settings.from_env(), True), timeout=1) == 0
            assert requested.count("/v1/responses") == 2
            assert len(sent) == 1
            assert not StateStore(configured_env).load().model("gpt-6-astra").notification_pending

    asyncio.run(run())
    assert stop_sent
    assert all(client.is_closed for client in clients)
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_invalid_probe_fails_once_without_sending_availability_alert(configured_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def handler(request):
        assert request.url.host == "agentrouter.test"
        if request.url.path == "/api/user/model-status":
            return httpx.Response(200, json={"success": True, "data": {"models": [{
                "name": "gpt-6-astra", "status": "operational", "current_tier": "ok",
                "apis": ["responses"],
            }]}})
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, text="<html>Maintenance</html>")

    clients = install_transport(monkeypatch, handler)
    assert cli.main(["--once"]) == 2
    record = StateStore(configured_env).load().model("gpt-6-astra")
    assert record.availability != "available"
    assert not record.notification_pending
    assert record.probe_error == "Probe did not return a valid Responses API response"
    assert record.last_probed_at is not None
    assert all(client.is_closed for client in clients)


def test_shutdown_reports_checkpoint_failure():
    async def run():
        stop = asyncio.Event()

        async def operation():
            try:
                stop.set()
                await asyncio.Event().wait()
            finally:
                raise OSError("Cannot save model progress")

        with pytest.raises(OSError, match="Cannot save model progress"):
            await cli._run_until_stopped(operation(), stop)

    asyncio.run(run())


def test_daemon_menu_handles_commands_queued_after_start(
    configured_env, monkeypatch, completed_response
):
    sent, menus, offsets, model_requests = [], [], [], []
    telegram = telegram_response(sent)
    acknowledged = 0

    def message(update_id, user_id, text):
        return {"update_id": update_id, "message": {
            "text": text, "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
        }}

    updates = [
        message(1, 42, "/start"),
        message(2, 42, "/status"),
        message(3, 42, "Probing Schedule"),
        message(4, 42, "Every 30 min"),
        message(5, 99, "/status"),
        message(6, 42, "Help"),
        message(7, 42, "/help@test_bot"),
    ]

    async def handler(request):
        nonlocal acknowledged
        if request.url.host == "telegram.test":
            method = request.url.path.rsplit("/", 1)[-1]
            data = json.loads(request.content)
            if method in {"setMyCommands", "setChatMenuButton"}:
                menus.append((method, data))
            if method == "getUpdates":
                offsets.append(data.get("offset"))
                acknowledged = max(acknowledged, data.get("offset", 0))
                remaining = [item for item in updates if item["update_id"] >= acknowledged]
                if not remaining:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    await asyncio.Event().wait()
                return httpx.Response(200, json={"ok": True, "result": remaining})
            return telegram(request)
        model_requests.append(request.url.path)
        if request.url.path == "/api/user/model-status":
            await asyncio.sleep(0)
            return httpx.Response(200, json={"success": True, "data": {"models": [{
                "name": "gpt-6-astra", "status": "operational", "current_tier": "ok",
                "apis": ["responses"],
            }]}})
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=completed_response)

    clients = install_transport(monkeypatch, handler)

    async def run():
        assert await asyncio.wait_for(cli._run(Settings.from_env(), False), timeout=2) == 0

    asyncio.run(run())
    texts = [item["text"] for item in sent]
    assert "gpt-6-astra ✅" in texts
    assert any(text.startswith("Probing Schedule") and "15 minutes" in text for text in texts)
    assert "Automatic probes every 30 minutes." in texts
    assert texts.count(TELEGRAM_HELP) == 2
    assert all(item["chat_id"] == "42" for item in sent)
    assert sent[0]["reply_markup"]["keyboard"] == [
        [{"text": "Status"}, {"text": "Probe Now"}],
        [{"text": "Balance & Usage"}, {"text": "Probing Schedule"}],
        [{"text": "Credit Alerts"}, {"text": "Help"}],
    ]
    assert menus[0][0] == "setMyCommands"
    assert menus[0][1]["scope"] == {"type": "chat", "chat_id": 42}
    assert [item["command"] for item in menus[0][1]["commands"]] == [
        "status", "balance", "probe_now", "probing_schedule", "credit_alerts", "help"
    ]
    assert menus[1] == ("setChatMenuButton", {"chat_id": 42, "menu_button": {"type": "commands"}})
    assert offsets == [None, 2, None, 8]
    assert model_requests == ["/api/user/model-status", "/v1/responses"]
    assert StateStore(configured_env).load().poll_seconds_override == 1800
    assert TelegramRegistrationStore(configured_env).load().chat_id == 42
    assert all(client.is_closed for client in clients)
