from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from .clients import ClientError, TelegramBot, TelegramClient, TransientClientError
from .config import ConfigurationError
from .messages import TELEGRAM_MENU
from .state import write_text_atomic


LOGGER = logging.getLogger(__name__)
CONNECTED_MESSAGE = "Connected. Choose an action to get started."
OWNERSHIP_OTP_PATTERN = re.compile(r"[0-9]{10}")
OWNERSHIP_OTP_TTL = 600
OWNERSHIP_MAX_CHALLENGES = 256
OWNERSHIP_OTP_PROMPT = "Send the one-time ownership code you set in .env. Attempts remaining: {attempts}."
OWNERSHIP_OTP_LOCKED = "Attempts remaining: 0. Restart the container to try again."
OWNERSHIP_OTP_EXPIRED = "The pairing window has expired. Restart the bot on the host to try again."
OWNERSHIP_FIELDS = {"TELEGRAM_CHAT_ID", "TELEGRAM_BOT_ID", "TELEGRAM_NEXT_UPDATE_ID"}
NON_TEXT_MESSAGE_FIELDS = {
    "animation", "audio", "caption", "caption_entities", "checklist", "contact",
    "dice", "document", "game", "live_photo", "location", "media_group_id",
    "paid_media", "photo", "poll", "sticker", "story", "venue", "video", "video_note",
    "voice",
}
T = TypeVar("T")


@dataclass(frozen=True)
class TelegramRegistration:
    bot_id: int
    chat_id: int
    owner_user_id: int
    # Non-null until the received updates are acknowledged and setup is confirmed.
    next_update_id: int | None = None


class TelegramRegistrationStore:
    def __init__(self, state_path: Path):
        self.path = state_path.parent / "ownership" / ".env"

    def load(self) -> TelegramRegistration | None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError:
            raise ConfigurationError(
                f"Invalid Telegram registration encoding: {self.path}. Restore the file, "
                "or stop the service and correct it before restarting."
            ) from None
        except OSError:
            raise ConfigurationError(f"Cannot read Telegram registration: {self.path}") from None
        try:
            fields: dict[str, str] = {}
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                key = key.strip()
                if not separator or key not in OWNERSHIP_FIELDS or key in fields:
                    raise ValueError("invalid ownership field")
                fields[key] = value.strip()
            chat = fields.get("TELEGRAM_CHAT_ID", "")
            if not chat:
                return None
            bot = fields.get("TELEGRAM_BOT_ID", "")
            offset = fields.get("TELEGRAM_NEXT_UPDATE_ID", "")
            if not re.fullmatch(r"[1-9][0-9]{0,18}", chat) or not re.fullmatch(r"[1-9][0-9]{0,18}", bot):
                raise ValueError("invalid ownership identifiers")
            if offset and not re.fullmatch(r"[0-9]{1,19}", offset):
                raise ValueError("invalid update offset")
            return TelegramRegistration(
                bot_id=int(bot), chat_id=int(chat), owner_user_id=int(chat),
                next_update_id=int(offset) if offset else None,
            )
        except ValueError:
            raise ConfigurationError(
                f"Invalid Telegram registration: {self.path}. Restore the file or clear "
                "TELEGRAM_CHAT_ID in it before restarting to pair again."
            ) from None

    def save(self, registration: TelegramRegistration) -> None:
        offset = registration.next_update_id
        try:
            write_text_atomic(
                self.path,
                "# Clear TELEGRAM_CHAT_ID and set a fresh OWNERSHIP_OTP in the project .env to pair again.\n"
                f"TELEGRAM_CHAT_ID={registration.chat_id}\n"
                f"TELEGRAM_BOT_ID={registration.bot_id}\n"
                f"TELEGRAM_NEXT_UPDATE_ID={offset if offset is not None else ''}\n",
            )
        except OSError:
            raise ConfigurationError(f"Cannot save Telegram registration: {self.path}") from None


async def retry_telegram(operation: Callable[[], Awaitable[T]], action: str = "Telegram setup") -> T:
    delay = 1
    while True:
        try:
            return await operation()
        except TransientClientError as exc:
            wait_seconds = max(delay, exc.retry_after or 0)
            LOGGER.warning("%s will retry in %ss: %s", action, wait_seconds, exc)
            await asyncio.sleep(wait_seconds)
            delay = min(delay * 2, 30)


def plain_message_text(message: dict[str, Any]) -> str | None:
    text = message.get("text")
    if not isinstance(text, str) or not NON_TEXT_MESSAGE_FIELDS.isdisjoint(message):
        return None
    return text


def _private_message(update: dict[str, Any]) -> tuple[int, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat"), message.get("from")
    text = plain_message_text(message)
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or not isinstance(sender, dict)
        or sender.get("is_bot") is not False
        or type(sender.get("id")) is not int
        or sender["id"] <= 0
        or type(chat.get("id")) is not int
        or chat["id"] != sender["id"]
        or text is None
        or not text.strip()
    ):
        return None
    return chat["id"], text


def _is_start(text: str, bot: TelegramBot) -> bool:
    command, separator, addressed_bot = text.split(maxsplit=1)[0].partition("@")
    return command == "/start" and (not separator or addressed_bot.lower() == bot.username.lower())


async def _pairing_reply(client: TelegramClient, chat_id: int, text: str) -> bool:
    try:
        await client.send(text, chat_id=str(chat_id))
    except ClientError:
        LOGGER.warning("Could not send a Telegram ownership reply")
        return False
    return True


async def _finish_registration(
    client: TelegramClient,
    store: TelegramRegistrationStore,
    registration: TelegramRegistration,
) -> None:
    client.chat_id = str(registration.chat_id)
    if registration.next_update_id is not None:
        # Acknowledge through the OTP, leaving later commands queued for the listener.
        await retry_telegram(lambda: client.get_updates(registration.next_update_id, timeout=0))
        await retry_telegram(lambda: client.send(CONNECTED_MESSAGE, reply_markup=TELEGRAM_MENU))
        store.save(replace(registration, next_update_id=None))
        LOGGER.info("Telegram recipient registered and saved")


async def _resolve(client: TelegramClient, state_path: Path) -> None:
    store = TelegramRegistrationStore(state_path)
    registration = store.load()
    bot = None
    if registration is not None:
        bot = await retry_telegram(client.get_me)
        if registration.bot_id == bot.id:
            await _finish_registration(client, store, registration)
            return
        LOGGER.info("Telegram bot changed; the owner must pair again")
    otp = client.settings.ownership_otp
    if otp is None or not OWNERSHIP_OTP_PATTERN.fullmatch(otp):
        raise ConfigurationError(
            "Set OWNERSHIP_OTP to a random 10-digit code or set TELEGRAM_CHAT_ID in .env"
        )
    if bot is None:
        bot = await retry_telegram(client.get_me)
    expires_at = monotonic() + OWNERSHIP_OTP_TTL
    LOGGER.info("Waiting for /start in a private chat with @%s for ownership pairing", bot.username)
    challenges: deque[int] = deque(maxlen=OWNERSHIP_MAX_CHALLENGES)
    attempts_left = client.settings.ownership_otp_max_attempts
    offset = None
    while True:
        updates = await retry_telegram(lambda: client.get_updates(offset))
        if not updates:
            await asyncio.sleep(0)
            continue
        offset = updates[-1]["update_id"] + 1
        for update in updates:
            private_message = _private_message(update)
            if private_message is None:
                continue
            chat_id, text = private_message
            if _is_start(text, bot):
                if attempts_left == 0:
                    await _pairing_reply(client, chat_id, OWNERSHIP_OTP_LOCKED)
                elif monotonic() >= expires_at:
                    await _pairing_reply(client, chat_id, OWNERSHIP_OTP_EXPIRED)
                elif chat_id not in challenges and await _pairing_reply(
                    client, chat_id, OWNERSHIP_OTP_PROMPT.format(attempts=attempts_left)
                ):
                    challenges.append(chat_id)
                continue
            if attempts_left == 0 or chat_id not in challenges:
                continue
            if monotonic() >= expires_at:
                challenges.remove(chat_id)
                await _pairing_reply(client, chat_id, OWNERSHIP_OTP_EXPIRED)
                continue
            if not OWNERSHIP_OTP_PATTERN.fullmatch(text) or not secrets.compare_digest(text, otp):
                attempts_left -= 1
                status = f"Attempts remaining: {attempts_left}." if attempts_left else OWNERSHIP_OTP_LOCKED
                await _pairing_reply(client, chat_id, f"Incorrect ownership code. {status}")
                continue
            registration = TelegramRegistration(bot.id, chat_id, chat_id, update["update_id"] + 1)
            # Persist the recipient before any reply or acknowledgment. A restart
            # during setup must resume this registration, never select another user.
            store.save(registration)
            await _finish_registration(client, store, registration)
            return


async def resolve_telegram_chat(
    client: TelegramClient, state_path: Path, stop_event: asyncio.Event
) -> bool:
    """Resolve the sole recipient, returning False if shutdown interrupts setup."""
    if stop_event.is_set():
        return False
    if client.settings.chat_id:
        return True
    registration_task = asyncio.create_task(_resolve(client, state_path))
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (registration_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        if registration_task in done:
            await registration_task
            return not stop_event.is_set()
        return False
    finally:
        for task in (registration_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(registration_task, stop_task, return_exceptions=True)
