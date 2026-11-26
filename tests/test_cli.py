import asyncio
import json
import os
import signal
import stat
from email.parser import BytesParser
from email.policy import default
from importlib.resources import files

import httpx
import pytest

from agentrouter_prober import cli, setup as credential_setup
from agentrouter_prober.config import Settings
from agentrouter_prober.messages import TELEGRAM_HELP
from agentrouter_prober.state import StateStore, format_datetime, health_is_fresh, utc_now
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


def install_transport(monkeypatch, handler, *, mock_validation=True):
    real_client = httpx.AsyncClient
    clients = []

    def respond(request):
        if mock_validation and request.url.host == "agentrouter.test":
            if request.url.path == "/v1/models":
                assert request.method == "GET"
                assert request.headers["Authorization"] == "Bearer sk-test"
                assert "New-Api-User" not in request.headers
                return httpx.Response(200, json={"data": [{"id": "gpt-6-astra"}]})
            if request.url.path == "/api/user/self":
                assert request.headers["Authorization"] == "Bearer dashboard-token"
                assert request.headers["New-Api-User"] == "60923"
                return httpx.Response(200, json={
                    "success": True, "data": {"id": 60923, "quota": 500_000, "used_quota": 0},
                })
            if request.url.path == "/api/status":
                return httpx.Response(200, json={"data": {"quota_per_unit": 500_000}})
        if request.url.host == "agentrouter.test" and request.url.path == "/api/token/":
            assert request.headers["Authorization"] == "Bearer dashboard-token"
            assert request.headers["New-Api-User"] == "60923"
            return httpx.Response(200, json={
                "success": True,
                "data": {"page": 1, "page_size": 20, "total": 1, "items": [{
                    "id": 1, "name": "Prober", "key": "test", "status": 1,
                    "unlimited_quota": True, "used_quota": 0,
                }]},
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


def test_bot_collects_missing_credentials_and_keeps_valid_values_when_a_check_fails(
    configured_env, monkeypatch, caplog, completed_response
):
    for name in ("AGENTROUTER_API_KEY", "CODEX_GATEWAY_API_KEY", "AGENTROUTER_USER_ID", "AGENTROUTER_DASHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env_path = configured_env.parent / "credentials" / ".env"
    sent, deleted, checks = [], [], []
    assets = files("agentrouter_prober").joinpath("assets")
    id_photo = "agentrouter-account-id.png"
    token_photo = "agentrouter-dashboard-token.png"
    photo_captions = {id_photo: [], token_photo: []}
    telegram = telegram_response(sent)
    probed = asyncio.Event()

    def message(update_id, text, user_id=42, **message_fields):
        return {"update_id": update_id, "message": {
            "message_id": update_id, "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
            **message_fields,
        }}

    updates = [
        message(1, "/start"), message(2, "/help"), message(3, "/balance"),
        message(4, "sk-other-user", user_id=99),
        message(5, None, photo=[{"file_id": "photo-id"}], caption="sk-caption"),
        message(6, "sk-with-document", document={"file_id": "document-id"}),
        message(7, "sk-test"), message(8, "wrong-id"),
        message(9, "60923", reply_to_message={"photo": [{"file_id": "guide-id"}]}),
        message(10, "dashboard-invalid"), message(11, "/api_keys"),
        message(12, "60923"), message(13, "dashboard-token"), message(14, "/balance"),
    ]
    acknowledged = 0

    async def handler(request):
        nonlocal acknowledged
        path = request.url.path
        if request.url.host == "telegram.test":
            method = path.rsplit("/", 1)[-1]
            if method == "getUpdates":
                data = json.loads(request.content)
                acknowledged = max(acknowledged, data.get("offset", 0))
                remaining = [item for item in updates if item["update_id"] >= acknowledged]
                if not remaining:
                    await probed.wait()
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    await asyncio.Event().wait()
                return httpx.Response(200, json={"ok": True, "result": remaining})
            if method == "deleteMessage":
                data = json.loads(request.content)
                assert data["chat_id"] == "42"
                deleted.append(data["message_id"])
                if data["message_id"] == 7:
                    return httpx.Response(400, json={"ok": False})
                return httpx.Response(200, json={"ok": True, "result": True})
            if method == "sendPhoto":
                headers = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n"
                form = BytesParser(policy=default).parsebytes(headers.encode() + request.content)
                parts = {part.get_param("name", header="content-disposition"): part for part in form.iter_parts()}
                assert parts["chat_id"].get_payload(decode=True) == b"42"
                assert parts["photo"].get_content_type() == "image/png"
                filename = parts["photo"].get_filename()
                assert filename in photo_captions
                assert parts["photo"].get_payload(decode=True) == assets.joinpath(filename).read_bytes()
                markup = json.loads(parts["reply_markup"].get_payload(decode=True))
                assert markup["keyboard"] == [[{"text": "↩️ Back"}]]
                caption = parts["caption"].get_payload(decode=True).decode()
                captions = photo_captions[filename]
                captions.append(caption)
                if "must be an integer" in caption or (filename == token_photo and len(captions) == 2):
                    return httpx.Response(400, json={"ok": False})
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
            return telegram(request)
        if path == "/v1/models":
            assert deleted[-1] in {10, 13}
            assert request.headers["authorization"] == "Bearer sk-test"
            checks.append(path)
            return httpx.Response(200, json={"data": [{"id": "gpt-6-astra"}]})
        if path == "/api/user/self":
            assert request.headers["new-api-user"] == "60923"
            if request.headers["authorization"] == "Bearer dashboard-invalid":
                assert not env_path.exists() and not configured_env.exists()
                return httpx.Response(401, json={"message": "dashboard-invalid"})
            assert request.headers["authorization"] == "Bearer dashboard-token"
            return httpx.Response(200, json={
                "success": True, "data": {"id": 60923, "quota": 500_000, "used_quota": 0},
            })
        if path == "/api/status":
            return httpx.Response(200, json={"data": {"quota_per_unit": 500_000}})
        assert env_path.is_file(), "Probes must wait until all credentials are verified and saved"
        if path == "/api/user/model-status":
            return httpx.Response(200, json={"success": True, "data": {"models": [{
                "name": "gpt-6-astra", "status": "operational", "current_tier": "ok",
                "apis": ["responses"],
            }]}})
        assert path == "/v1/responses"
        probed.set()
        return httpx.Response(200, json=completed_response)

    clients = install_transport(monkeypatch, handler, mock_validation=False)
    assert cli.main(["--healthcheck"]) == 0
    asyncio.run(asyncio.wait_for(cli._run(Settings.from_env(), False), timeout=2))
    texts = "\n".join(item["text"] for item in sent)
    assert TELEGRAM_HELP in texts
    assert texts.count("Send your AgentRouter API key for probing models (sk-…).") == 1
    assert "AGENTROUTER_USER_ID must be an integer" in texts
    assert "Personal Settings" in texts
    id_captions = photo_captions[id_photo]
    assert len(id_captions) == 4 and all("digits next to ID" in caption for caption in id_captions)
    assert any("AgentRouter rejected the dashboard token and account ID pair" in caption for caption in id_captions)
    token_captions = photo_captions[token_photo]
    assert len(token_captions) == 2
    for prompt in [*token_captions, texts]:
        assert "Personal Settings → Security Settings" in prompt
        assert "Generate Token under System Access Token" in prompt
        assert "https://agentrouter.org/console" in prompt
    assert "credentials verified and saved" in texts
    assert "Telegram could not delete your credential message. Delete it manually" in texts
    assert "💰 $1.00 available" in texts
    assert deleted == [7, 8, 9, 10, 12, 13]
    assert checks == ["/v1/models", "/v1/models"] and probed.is_set()
    all_output = "\n".join([texts, *id_captions, *token_captions, caplog.text])
    assert all(secret not in all_output for secret in ("sk-test", "dashboard-token", "dashboard-invalid"))
    if os.name == "posix":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    monkeypatch.setenv("AGENTROUTER_DASHBOARD_TOKEN", "dashboard-invalid")
    restored = Settings.from_env(require_agentrouter=True)
    assert (restored.agentrouter_api_key, restored.agentrouter_user_id, restored.agentrouter_dashboard_token) == (
        "sk-test", 60923, "dashboard-token",
    )
    assert asyncio.run(cli._run(restored, True)) == 0
    assert checks == ["/v1/models"] * 3
    assert all(client.is_closed for client in clients)


def test_setup_retries_a_model_key_rejection_temporary_failure_and_unsaved_credentials(
    configured_env, monkeypatch
):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-rejected")
    model_responses = iter((401, 503, 200, 200))

    def handler(request):
        if request.url.path == "/v1/models":
            status = next(model_responses)
            assert request.headers["authorization"] == ("Bearer sk-rejected" if status == 401 else "Bearer sk-test")
            return httpx.Response(status, json={"data": []})
        assert request.url.path == "/api/user/self"
        assert request.headers["authorization"] == "Bearer dashboard-token"
        assert request.headers["New-Api-User"] == "60923"
        return httpx.Response(200, json={
            "success": True, "data": {"id": 60923, "quota": 500_000, "used_quota": 0},
        })

    clients = install_transport(monkeypatch, handler, mock_validation=False)
    env_path = configured_env.parent / "credentials" / ".env"

    def fail_save(*args):
        raise OSError("Cannot save credentials")

    async def run():
        setup = credential_setup.AgentRouterSetup(Settings.from_env())
        assert await setup.connect() is None
        assert setup.next_field == "AGENTROUTER_API_KEY"
        assert "rejected the model API key" in setup.problem
        assert setup.retry_after is None
        setup.submit("sk-test")
        assert await setup.connect() is None
        assert setup.next_field is None
        assert "Could not verify the model API key" in setup.problem
        assert setup.retry_after == 0
        with monkeypatch.context() as patch:
            patch.setattr(credential_setup, "save_agentrouter_credentials", fail_save)
            assert await setup.connect() is None
            assert setup.next_field is None
            assert "Could not save the verified credentials" in setup.problem
            assert setup.retry_after is None
            assert not env_path.exists()
        client = await setup.connect()
        assert client is not None and env_path.is_file()
        await client.close()

    asyncio.run(run())
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("chat_id", ["42", "-98765"])
def test_daemon_recovers_from_temporary_startup_validation_failure(configured_env, monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    model_statuses = iter((503, 200))
    telegram = telegram_response([])
    monitoring_started = False

    async def handler(request):
        if request.url.host == "telegram.test":
            if request.url.path.endswith("/getUpdates"):
                await asyncio.Event().wait()
            return telegram(request)
        if request.url.path == "/v1/models":
            return httpx.Response(next(model_statuses), json={"data": []})
        assert request.url.path == "/api/user/self"
        return httpx.Response(200, json={
            "success": True, "data": {"id": 60923, "quota": 500_000, "used_quota": 0},
        })

    async def monitor(self, stop_event):
        nonlocal monitoring_started
        monitoring_started = True
        stop_event.set()

    clients = install_transport(monkeypatch, handler, mock_validation=False)
    monkeypatch.setattr(cli.Prober, "run_forever", monitor)
    assert asyncio.run(asyncio.wait_for(cli._run(Settings.from_env(), False), timeout=5)) == 0
    assert monitoring_started
    assert next(model_statuses, None) is None
    assert all(client.is_closed for client in clients)


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


def test_healthcheck_rejects_missing_telegram_configuration_with_fresh_state(configured_env, monkeypatch):
    configured_env.write_text(json.dumps({"last_successful_status_at": format_datetime(utc_now())}))
    assert cli.main(["--healthcheck"]) == 0
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert cli.main(["--healthcheck"]) == 2


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
        message(3, 42, "⏱️ Schedule"),
        message(4, 42, "Every 30 min"),
        message(5, 99, "/status"),
        message(6, 42, "❓ Help"),
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
    assert "📡 Model Status\n\n✅ gpt-6-astra" in texts
    assert any(text.startswith("⏱️ Probing Schedule") and "15 minutes" in text for text in texts)
    assert "✅ Automatic probes every 30 minutes" in texts
    assert texts.count(TELEGRAM_HELP) == 2
    assert all(item["chat_id"] == "42" for item in sent)
    assert sent[0]["reply_markup"]["keyboard"] == [
        [{"text": "📡 Status"}, {"text": "⚡ Probe Now"}],
        [{"text": "💰 Balance"}, {"text": "🔑 API Keys"}],
        [{"text": "🔔 Credit Alerts"}, {"text": "📊 Weekly Digest"}],
        [{"text": "⏱️ Schedule"}, {"text": "❓ Help"}],
    ]
    assert menus[0][0] == "setMyCommands"
    assert menus[0][1]["scope"] == {"type": "chat", "chat_id": 42}
    assert [item["command"] for item in menus[0][1]["commands"]] == [
        "status", "probe_now", "balance", "api_keys", "credit_alerts", "weekly_digest",
        "probing_schedule", "help",
    ]
    assert menus[1] == ("setChatMenuButton", {"chat_id": 42, "menu_button": {"type": "commands"}})
    assert offsets == [None, 2, None, 8]
    assert model_requests == ["/api/user/model-status", "/v1/responses"]
    assert StateStore(configured_env).load().poll_seconds_override == 1800
    assert TelegramRegistrationStore(configured_env).load().chat_id == 42
    assert all(client.is_closed for client in clients)
