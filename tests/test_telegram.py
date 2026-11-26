import asyncio
import json
import logging
import os

import httpx
import pytest

from agentrouter_prober import cli
from agentrouter_prober import telegram as registration_module
from agentrouter_prober.clients import (
    AuthenticationError,
    ProtocolError,
    TelegramClient,
    TransientClientError,
)
from agentrouter_prober.config import ConfigurationError, TelegramSettings
from agentrouter_prober.telegram import (
    CONNECTED_MESSAGE,
    TelegramRegistration,
    TelegramRegistrationStore,
    resolve_telegram_chat,
)


TOKEN = "123:private-test-token"


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def identity(bot_id=123):
    return ok({"id": bot_id, "is_bot": True, "username": "prober_bot"})


def update(update_id, user_id=42, text="/start", chat_type="private", is_bot=False, **message_fields):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot},
            **message_fields,
        },
    }


def request_data(request):
    return request.url.path.rsplit("/", 1)[-1], json.loads(request.content)


def settings(chat_id=None, token=TOKEN):
    return TelegramSettings(token, chat_id, "https://telegram.test")


@pytest.mark.parametrize("chat_id", [None, "", "  "])
def test_chat_id_is_optional(monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    if chat_id is not None:
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    assert TelegramSettings.from_env().chat_id is None


@pytest.mark.parametrize("chat_id", ["-98765", "+42", "@example_channel"])
def test_explicit_chat_id_is_preserved(monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f" {chat_id} ")
    assert TelegramSettings.from_env().chat_id == chat_id


@pytest.mark.parametrize("chat_id", ["typo", "0", "@bad name"])
def test_invalid_explicit_chat_id_is_rejected(monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    with pytest.raises(ConfigurationError, match="TELEGRAM_CHAT_ID"):
        TelegramSettings.from_env()


def test_bot_token_remains_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN is required"):
        TelegramSettings.from_env()


def test_first_private_start_wins_and_survives_restart_and_token_rotation(state_directory):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    requests, sent = [], []
    updates = [
        update(11, 99),  # Out of order: the earlier valid sender still wins.
        update(1, chat_type="group"),
        update(2, is_bot=True),
        update(3, text="hello"),
        update(4, text="/starter"),
        update(5, text="/start@another_bot"),
        {"update_id": 6, "edited_message": update(6)["message"]},
        update(7, text="/start@"),
        update(8, user_id=98, text=None, photo=[{"file_id": "photo-id"}], caption="/start"),
        update(9, user_id=99, document={"file_id": "document-id"}),
        update(10, 42, text="/start@PROBER_BOT payload"),
    ]

    def handler(request):
        method, data = request_data(request)
        requests.append(method)
        if method == "getMe":
            return identity()
        if method == "getUpdates":
            assert data["allowed_updates"] == ["message"]
            if "offset" not in data:
                assert data["timeout"] == 10
                assert request.extensions["timeout"]["read"] >= 15
                return ok(updates)
            assert data["offset"] == 11 and data["timeout"] == 0
            assert store.load().chat_id == 42  # Saved before acknowledging Telegram.
            return ok([update(12, 111)])  # Cannot replace the registered sender.
        assert method == "sendMessage"
        assert store.load().chat_id == 42  # Saved before any message is sent.
        sent.append(data)
        return ok({"message_id": len(sent)})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), timeout_seconds=1, http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())
            await client.send("Availability alert")
            assert store.load() == TelegramRegistration(123, 42, 42)
            before_restart = len(requests)
            restarted = TelegramClient(settings(token="123:rotated-token"), http_client=http)
            assert await resolve_telegram_chat(restarted, path, asyncio.Event())
            assert requests[before_restart:] == ["getMe"]
            await restarted.send("Another alert")

    asyncio.run(run())
    assert [item["chat_id"] for item in sent] == ["42", "42", "42"]
    assert sent[0]["text"] == CONNECTED_MESSAGE
    assert TOKEN not in store.path.read_text()


def test_empty_polls_and_ignored_updates_advance_to_a_new_start(state_directory):
    offsets = []

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity()
        if method == "sendMessage":
            return ok({})
        offsets.append(data.get("offset"))
        return ok([[], [update(10, text="hello")], [update(20)], []][len(offsets) - 1])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await resolve_telegram_chat(client, state_directory / "state.json", asyncio.Event())
            assert client.chat_id == "42"

    asyncio.run(run())
    assert offsets == [None, None, 11, 21]


def test_explicit_chat_bypasses_identity_polling_and_saved_registration(state_directory):
    path = state_directory / "state.json"
    TelegramRegistrationStore(path).path.write_text("corrupt")
    sent = []

    def handler(request):
        method, data = request_data(request)
        assert method == "sendMessage"
        sent.append(data["chat_id"])
        return ok({})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings("-999"), http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())
            await client.send("Test")

    asyncio.run(run())
    assert sent == ["-999"]


def test_different_bot_requires_new_registration(state_directory):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    store.save(TelegramRegistration(100, 50, 50))
    sent = []

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity(200)
        if method == "getUpdates":
            return ok([] if "offset" in data else [update(10, 60)])
        sent.append(data["chat_id"])
        return ok({})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())

    asyncio.run(run())
    assert sent == ["60"]
    assert store.load() == TelegramRegistration(200, 60, 60)


@pytest.mark.parametrize("content", [
    b"not json", b"[]", b"\xff", b'{"version":2}',
    b'{"version":1,"bot_id":123,"chat_id":42,"owner_user_id":99,"next_update_id":null}',
    b'{"version":1,"bot_id":123,"chat_id":true,"owner_user_id":true,"next_update_id":null}',
    b'{"version":1,"bot_id":123,"chat_id":42,"owner_user_id":42,"next_update_id":-1}',
])
def test_corrupt_registration_fails_without_reopening_setup(state_directory, content):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    store.path.write_bytes(content)

    def handler(request):
        raise AssertionError("Corrupt registration must fail before contacting Telegram")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            with pytest.raises(ConfigurationError, match="Invalid Telegram registration"):
                await resolve_telegram_chat(client, path, asyncio.Event())

    asyncio.run(run())
    assert store.path.read_bytes() == content


def test_failed_atomic_save_preserves_previous_binding_and_sends_nothing(state_directory, monkeypatch):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    previous = TelegramRegistration(100, 50, 50)
    store.save(previous)

    def cannot_replace(source, destination):
        raise PermissionError("simulated unwritable storage")

    monkeypatch.setattr(os, "replace", cannot_replace)

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity()
        assert method == "getUpdates" and "offset" not in data
        return ok([update(10)])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            with pytest.raises(ConfigurationError, match="Cannot save"):
                await resolve_telegram_chat(client, path, asyncio.Event())

    asyncio.run(run())
    assert store.load() == previous


def test_interrupted_registration_resumes_with_the_same_sender(state_directory):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    sent = []

    async def run():
        stop = asyncio.Event()
        acknowledgement_started = asyncio.Event()

        async def interrupted_handler(request):
            method, data = request_data(request)
            if method == "getMe":
                return identity()
            assert method == "getUpdates"
            if "offset" not in data:
                return ok([update(10, 42), update(11, 99)])
            acknowledgement_started.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted_handler)) as http:
            task = asyncio.create_task(resolve_telegram_chat(
                TelegramClient(settings(), http_client=http), path, stop
            ))
            await asyncio.wait_for(acknowledgement_started.wait(), 1)
            stop.set()
            assert not await asyncio.wait_for(task, 1)
        assert store.load() == TelegramRegistration(123, 42, 42, 11)

        def restarted_handler(request):
            method, data = request_data(request)
            if method == "getMe":
                return identity()
            if method == "getUpdates":
                assert data["offset"] == 11 and data["timeout"] == 0
                return ok([update(12, 99)])
            sent.append(data["chat_id"])
            return ok({})

        async with httpx.AsyncClient(transport=httpx.MockTransport(restarted_handler)) as http:
            assert await resolve_telegram_chat(
                TelegramClient(settings(), http_client=http), path, asyncio.Event()
            )

    asyncio.run(run())
    assert sent == ["42"]
    assert store.load() == TelegramRegistration(123, 42, 42)


def test_setup_retries_use_capped_backoff_and_respect_retry_after(state_directory, monkeypatch):
    waits = []
    attempts = 0
    store = TelegramRegistrationStore(state_directory / "state.json")
    store.save(TelegramRegistration(123, 42, 42))

    async def record_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(registration_module.asyncio, "sleep", record_sleep)

    def handler(request):
        nonlocal attempts
        method, _ = request_data(request)
        assert method == "getMe"
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout(f"Request failed: {request.url}", request=request)
        if attempts <= 7:
            return httpx.Response(503, text="Unavailable")
        if attempts == 8:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 90}})
        return identity()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await resolve_telegram_chat(
                TelegramClient(settings(), http_client=http), state_directory / "state.json", asyncio.Event()
            )

    asyncio.run(run())
    assert waits == [1, 2, 4, 8, 16, 30, 30, 90]


@pytest.mark.parametrize("during_backoff", [False, True])
def test_waiting_for_start_or_retry_can_be_cancelled(state_directory, during_backoff):
    async def run():
        stop = asyncio.Event()
        polling_started = asyncio.Event()

        async def handler(request):
            method, _ = request_data(request)
            if method == "getMe":
                return identity()
            polling_started.set()
            if during_backoff:
                return httpx.Response(429, json={"parameters": {"retry_after": 3600}})
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            task = asyncio.create_task(resolve_telegram_chat(
                TelegramClient(settings(), http_client=http), state_directory / "state.json", stop
            ))
            await asyncio.wait_for(polling_started.wait(), 1)
            stop.set()
            assert not await asyncio.wait_for(task, 1)
        assert not (state_directory / "telegram.json").exists()

    asyncio.run(run())


@pytest.mark.parametrize("status,payload,error,match", [
    (401, {}, AuthenticationError, "token"),
    (403, {}, AuthenticationError, "token"),
    (404, {}, AuthenticationError, "token"),
    (409, {}, ProtocolError, "webhook or another bot instance"),
    (400, {}, ProtocolError, "HTTP 400"),
    (500, {}, TransientClientError, "error 500"),
    (200, {"ok": False, "error_code": 409}, ProtocolError, "webhook"),
    (200, {"ok": False, "error_code": 429}, TransientClientError, "rate limit"),
    (200, {"ok": True}, ProtocolError, "result"),
    (200, {"ok": True, "result": {}}, ProtocolError, "identity"),
])
def test_telegram_api_errors_are_actionable_and_do_not_expose_token(status, payload, error, match):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={**payload, "description": TOKEN})
        )) as http:
            with pytest.raises(error, match=match) as raised:
                await TelegramClient(settings(), http_client=http).get_me()
            assert TOKEN not in str(raised.value)

    asyncio.run(run())


@pytest.mark.parametrize("body,match", [
    ("not json", "invalid JSON"),
    ('{"ok":true,"result":{}}', "invalid updates"),
    ('{"ok":true,"result":[{"update_id":true}]}', "invalid updates"),
])
def test_invalid_update_responses_fail_cleanly(body, match):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )) as http:
            with pytest.raises(ProtocolError, match=match):
                await TelegramClient(settings(), http_client=http).get_updates()

    asyncio.run(run())


def test_cli_disables_transport_logs_containing_credentials(monkeypatch, caplog):
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    caplog.set_level(logging.DEBUG)
    cli._configure_logging()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: identity())) as http:
            await TelegramClient(settings(), http_client=http).get_me()

    asyncio.run(run())
    assert TOKEN not in caplog.text
    assert logging.getLogger("httpcore").level == logging.WARNING
