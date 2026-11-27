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
    OWNERSHIP_OTP_EXPIRED,
    OWNERSHIP_OTP_LOCKED,
    OWNERSHIP_OTP_PROMPT,
    TelegramRegistration,
    TelegramRegistrationStore,
    resolve_telegram_chat,
)


TOKEN = "123:private-test-token"
OTP = "0314159265"


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


def settings(chat_id=None, token=TOKEN, ownership_otp=OTP):
    return TelegramSettings(token, chat_id, "https://telegram.test", ownership_otp=ownership_otp)


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


def test_only_the_private_chat_with_the_code_wins_and_survives_restart_and_token_rotation(state_directory, capsys):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    requests, sent = [], []
    updates = [
        update(18, 99),  # Out of order: pairing consumes only through the valid code.
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
        update(11, 99, text=OTP),  # No /start in this chat.
        update(12, 101),
        update(13, 101, text="0000000000"),
        update(14, 42, text=OTP, document={"file_id": "document-id"}),
        update(15, 42, text=OTP, chat_type="group"),
        update(16, 42, text=OTP),
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
            assert data["offset"] == 17 and data["timeout"] == 0
            assert store.load().chat_id == 42  # Saved before acknowledging Telegram.
            return ok([update(12, 111)])  # Cannot replace the registered sender.
        assert method == "sendMessage"
        if data["text"] == CONNECTED_MESSAGE:
            assert store.load().chat_id == 42
        sent.append(data)
        return ok({"message_id": len(sent)})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), timeout_seconds=1, http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())
            await client.send("Availability alert")
            assert store.load() == TelegramRegistration(123, 42, 42)
            before_restart = len(requests)
            restarted = TelegramClient(settings(token="123:rotated-token", ownership_otp=None), http_client=http)
            assert await resolve_telegram_chat(restarted, path, asyncio.Event())
            assert requests[before_restart:] == ["getMe"]
            await restarted.send("Another alert")

    asyncio.run(run())
    assert [(item["chat_id"], item["text"]) for item in sent[:4]] == [
        ("42", OWNERSHIP_OTP_PROMPT.format(attempts=10)),
        ("101", OWNERSHIP_OTP_PROMPT.format(attempts=10)),
        ("101", "Incorrect ownership code. Attempts remaining: 9."), ("42", CONNECTED_MESSAGE),
    ]
    assert [item["chat_id"] for item in sent[4:]] == ["42", "42"]
    assert all(OTP not in item["text"] for item in sent)
    assert OTP not in capsys.readouterr().out
    assert TOKEN not in store.path.read_text()
    assert OTP not in store.path.read_text()
    assert "TELEGRAM_CHAT_ID=42\n" in store.path.read_text()


def test_empty_polls_and_ignored_updates_advance_to_a_new_start(state_directory):
    offsets = []

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity()
        if method == "sendMessage":
            return ok({})
        offsets.append(data.get("offset"))
        return ok([[], [update(10, text="hello")], [update(20), update(21, text=OTP)], []][len(offsets) - 1])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await resolve_telegram_chat(client, state_directory / "state.json", asyncio.Event())
            assert client.chat_id == "42"

    asyncio.run(run())
    assert offsets == [None, None, 11, 22]


@pytest.mark.parametrize("otp", [None, "short-code", "123456789٠"])
def test_unpaired_bot_rejects_missing_or_invalid_code_before_contacting_telegram(
    state_directory, otp, caplog, capsys,
):
    def handler(request):
        pytest.fail("Unpaired bot contacted Telegram with invalid ownership configuration")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(ownership_otp=otp), http_client=http)
            with pytest.raises(ConfigurationError, match="Set OWNERSHIP_OTP"):
                await resolve_telegram_chat(client, state_directory / "state.json", asyncio.Event())

    asyncio.run(run())
    if otp:
        assert otp not in caplog.text
        assert otp not in capsys.readouterr().out


@pytest.mark.parametrize("configured_limit, limit", [(None, 10), ("3", 3)])
def test_wrong_codes_exhaust_a_shared_budget_without_resetting_on_start(
    state_directory, monkeypatch, configured_limit, limit, caplog,
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("OWNERSHIP_OTP", OTP)
    monkeypatch.delenv("OWNERSHIP_OTP_MAX_ATTEMPTS", raising=False)
    if configured_limit is not None:
        monkeypatch.setenv("OWNERSHIP_OTP_MAX_ATTEMPTS", configured_limit)
    path = state_directory / "state.json"
    sent = []
    updates = [
        update(1, 42),
        update(2, 42, text=f"{OTP};"),
        update(3, 42),
        update(4, 99),
        update(5, 99, text=f"{OTP}\n"),
        *[update(index, 99, text="0000000000") for index in range(6, limit + 4)],
        update(limit + 4, 160),
        update(limit + 5, 42, text=OTP),
        update(limit + 6, 160, text=OTP),
    ]

    async def run():
        stop = asyncio.Event()

        async def handler(request):
            method, data = request_data(request)
            if method == "getMe":
                return identity()
            if method == "sendMessage":
                sent.append((data["chat_id"], data["text"]))
                return ok({})
            assert method == "getUpdates"
            if "offset" not in data:
                return ok(updates)
            assert data["offset"] == limit + 7
            stop.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(TelegramSettings.from_env(), http_client=http)
            assert not await asyncio.wait_for(resolve_telegram_chat(client, path, stop), 1)
            assert client.chat_id is None

    asyncio.run(run())
    assert sent == [
        ("42", OWNERSHIP_OTP_PROMPT.format(attempts=limit)),
        ("42", f"Incorrect ownership code. Attempts remaining: {limit - 1}."),
        ("99", OWNERSHIP_OTP_PROMPT.format(attempts=limit - 1)),
        *[("99", f"Incorrect ownership code. Attempts remaining: {left}.")
          for left in range(limit - 2, 0, -1)],
        ("99", f"Incorrect ownership code. {OWNERSHIP_OTP_LOCKED}"),
        ("160", OWNERSHIP_OTP_LOCKED),
    ]
    assert OTP not in caplog.text
    assert TelegramRegistrationStore(path).load() is None


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_ownership_attempt_limit_must_be_positive(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNERSHIP_OTP_MAX_ATTEMPTS", value)
    with pytest.raises(ConfigurationError, match="OWNERSHIP_OTP_MAX_ATTEMPTS"):
        TelegramSettings.from_env()


def test_start_can_retry_after_the_ownership_prompt_fails(state_directory):
    path = state_directory / "state.json"
    sent = []

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity()
        if method == "getUpdates":
            if "offset" not in data:
                return ok([update(10), update(11), update(12, text=OTP)])
            assert data["offset"] == 13 and data["timeout"] == 0
            return ok([])
        sent.append(data["text"])
        if len(sent) == 1:
            return httpx.Response(503, text="Unavailable")
        return ok({})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await asyncio.wait_for(resolve_telegram_chat(client, path, asyncio.Event()), 1)

    asyncio.run(run())
    assert sent == [OWNERSHIP_OTP_PROMPT.format(attempts=10)] * 2 + [CONNECTED_MESSAGE]
    assert TelegramRegistrationStore(path).load() == TelegramRegistration(123, 42, 42)


def test_explicit_chat_bypasses_identity_polling_and_saved_registration(state_directory):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    store.path.parent.mkdir()
    store.path.write_text("corrupt")
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
            return ok([] if "offset" in data else [update(10, 60), update(11, 60, text=OTP)])
        sent.append(data["chat_id"])
        return ok({})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())

    asyncio.run(run())
    assert sent == ["60", "60"]
    assert store.load() == TelegramRegistration(200, 60, 60)


@pytest.mark.parametrize("content", [
    b"invalid file", b"\xff", b"TELEGRAM_CHAT_ID=42\n",
    b"TELEGRAM_CHAT_ID=true\nTELEGRAM_BOT_ID=123\n",
    b"TELEGRAM_CHAT_ID=42\nTELEGRAM_BOT_ID=123\nTELEGRAM_NEXT_UPDATE_ID=-1\n",
    b"TELEGRAM_CHAT_ID=42\nTELEGRAM_CHAT_ID=99\nTELEGRAM_BOT_ID=123\n",
])
def test_corrupt_registration_fails_without_reopening_setup(state_directory, content):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    store.path.parent.mkdir()
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


def test_clearing_the_saved_chat_id_reopens_pairing(state_directory, capsys):
    path = state_directory / "state.json"
    store = TelegramRegistrationStore(path)
    store.save(TelegramRegistration(123, 42, 42))
    store.path.write_text(store.path.read_text().replace("TELEGRAM_CHAT_ID=42", "TELEGRAM_CHAT_ID="))
    (state_directory / "telegram.json").write_text('{"bot_id":123,"chat_id":42}')

    def handler(request):
        method, data = request_data(request)
        if method == "getMe":
            return identity()
        if method == "getUpdates":
            return ok([] if "offset" in data else [update(20, 99), update(21, 99, text=OTP)])
        assert data["chat_id"] == "99"
        return ok({})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert await resolve_telegram_chat(client, path, asyncio.Event())
            assert client.chat_id == "99"

    asyncio.run(run())
    assert store.path == state_directory / "ownership" / ".env"
    assert store.load() == TelegramRegistration(123, 99, 99)
    assert OTP not in capsys.readouterr().out


def test_expired_code_cannot_claim_ownership(state_directory, monkeypatch):
    sent = []
    now = 0
    monkeypatch.setattr(registration_module, "monotonic", lambda: now)
    path = state_directory / "state.json"

    async def run():
        stop = asyncio.Event()

        async def handler(request):
            nonlocal now
            method, data = request_data(request)
            if method == "getMe":
                return identity()
            if method == "sendMessage":
                sent.append(data["text"])
                return ok({})
            if "offset" not in data:
                return ok([update(10)])
            if data["offset"] == 11:
                now = registration_module.OWNERSHIP_OTP_TTL
                return ok([update(11, text=OTP)])
            stop.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TelegramClient(settings(), http_client=http)
            assert not await asyncio.wait_for(resolve_telegram_chat(client, path, stop), 1)
            assert client.chat_id is None

    asyncio.run(run())
    assert sent == [OWNERSHIP_OTP_PROMPT.format(attempts=10), OWNERSHIP_OTP_EXPIRED]
    assert TelegramRegistrationStore(path).load() is None


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
        if method == "sendMessage":
            assert data["text"] == OWNERSHIP_OTP_PROMPT.format(attempts=10)
            return ok({})
        assert method == "getUpdates" and "offset" not in data
        return ok([update(10), update(11, text=OTP)])

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
            if method == "sendMessage":
                assert data["text"] == OWNERSHIP_OTP_PROMPT.format(attempts=10)
                return ok({})
            assert method == "getUpdates"
            if "offset" not in data:
                return ok([update(10, 42), update(11, 99), update(12, 42, text=OTP)])
            acknowledgement_started.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted_handler)) as http:
            task = asyncio.create_task(resolve_telegram_chat(
                TelegramClient(settings(), http_client=http), path, stop
            ))
            await asyncio.wait_for(acknowledgement_started.wait(), 1)
            stop.set()
            assert not await asyncio.wait_for(task, 1)
        assert store.load() == TelegramRegistration(123, 42, 42, 13)

        def restarted_handler(request):
            method, data = request_data(request)
            if method == "getMe":
                return identity()
            if method == "getUpdates":
                assert data["offset"] == 13 and data["timeout"] == 0
                return ok([update(14, 99)])
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
        assert TelegramRegistrationStore(state_directory / "state.json").load() is None

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
