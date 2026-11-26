from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from .clients import TelegramBot, TelegramClient, TransientClientError
from .config import ConfigurationError
from .messages import TELEGRAM_MENU


LOGGER = logging.getLogger(__name__)
CONNECTED_MESSAGE = "Connected. Choose an action to get started."
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
        self.path = state_path.with_name("telegram.json")
        if self.path == state_path:
            raise ConfigurationError(
                "STATE_PATH must not use telegram.json; it is reserved for Telegram registration"
            )

    def load(self) -> TelegramRegistration | None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError:
            raise ConfigurationError(
                f"Invalid Telegram registration encoding: {self.path}. Restore the file, "
                "or stop the service and remove it deliberately to register again."
            ) from None
        except OSError:
            raise ConfigurationError(f"Cannot read Telegram registration: {self.path}") from None
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
                raise ValueError("unsupported registration")
            if any(type(raw.get(key)) is not int or raw[key] <= 0 for key in (
                "bot_id", "chat_id", "owner_user_id"
            )):
                raise ValueError("invalid registration IDs")
            if raw["chat_id"] != raw["owner_user_id"]:
                raise ValueError("registration is not a private chat")
            next_update_id = raw["next_update_id"]
            if next_update_id is not None and (
                type(next_update_id) is not int or next_update_id < 0
            ):
                raise ValueError("invalid update offset")
            return TelegramRegistration(
                bot_id=raw["bot_id"],
                chat_id=raw["chat_id"],
                owner_user_id=raw["owner_user_id"],
                next_update_id=next_update_id,
            )
        except (ValueError, KeyError, TypeError):
            raise ConfigurationError(
                f"Invalid Telegram registration: {self.path}. Restore the file, or stop "
                "the service and remove it deliberately to register a recipient again."
            ) from None

    def save(self, registration: TelegramRegistration) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({"version": 1, **asdict(registration)}, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            raise ConfigurationError(f"Cannot save Telegram registration: {self.path}") from None


async def _retry(operation: Callable[[], Awaitable[T]], action: str = "Telegram setup") -> T:
    delay = 1
    while True:
        try:
            return await operation()
        except TransientClientError as exc:
            wait_seconds = max(delay, exc.retry_after or 0)
            LOGGER.warning("%s will retry in %ss: %s", action, wait_seconds, exc)
            await asyncio.sleep(wait_seconds)
            delay = min(delay * 2, 30)


def _plain_message_text(message: dict[str, Any]) -> str | None:
    text = message.get("text")
    if not isinstance(text, str) or not NON_TEXT_MESSAGE_FIELDS.isdisjoint(message):
        return None
    return text


def _private_start(update: dict[str, Any], bot: TelegramBot) -> int | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender = message.get("chat"), message.get("from")
    text = _plain_message_text(message)
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
        or not text.startswith("/")
    ):
        return None
    command, separator, addressed_bot = text.split(maxsplit=1)[0].partition("@")
    if command != "/start" or (
        separator and addressed_bot.lower() != bot.username.lower()
    ):
        return None
    return chat["id"]


async def _finish_registration(
    client: TelegramClient,
    store: TelegramRegistrationStore,
    registration: TelegramRegistration,
) -> None:
    client.chat_id = str(registration.chat_id)
    if registration.next_update_id is not None:
        # Acknowledge through /start, leaving later commands queued for the listener.
        await _retry(lambda: client.get_updates(registration.next_update_id, timeout=0))
        await _retry(lambda: client.send(CONNECTED_MESSAGE, reply_markup=TELEGRAM_MENU))
        store.save(replace(registration, next_update_id=None))
        LOGGER.info("Telegram recipient registered and saved")


async def _resolve(client: TelegramClient, state_path: Path) -> None:
    store = TelegramRegistrationStore(state_path)
    registration = store.load()
    bot = await _retry(client.get_me)
    if registration is not None and registration.bot_id == bot.id:
        await _finish_registration(client, store, registration)
        return
    if registration is not None:
        LOGGER.info("Telegram bot changed; a new recipient must press Start")
    LOGGER.info("Waiting for /start in a private chat with @%s", bot.username)
    offset = None
    while True:
        updates = await _retry(lambda: client.get_updates(offset))
        if not updates:
            await asyncio.sleep(0)
            continue
        offset = updates[-1]["update_id"] + 1
        for update in updates:
            chat_id = _private_start(update, bot)
            if chat_id is None:
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
