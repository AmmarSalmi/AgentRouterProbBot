from __future__ import annotations

import asyncio
import logging
from typing import Any

from .app import Prober
from .clients import ClientError, TransientClientError
from .messages import (
    SCHEDULE_CHOICES,
    TELEGRAM_COMMANDS,
    TELEGRAM_MENU,
    TELEGRAM_SCHEDULE_MENU,
    account_usage_message,
    format_duration,
    probing_schedule_message,
    status_message,
    telegram_message_parts,
)
from .telegram import _retry


LOGGER = logging.getLogger(__name__)


class TelegramCommands:
    def __init__(self, prober: Prober):
        self.prober = prober
        self.telegram = prober.telegram
        try:
            chat_id = int(self.telegram.chat_id or "")
        except ValueError:
            chat_id = 0
        self.owner_id = chat_id if chat_id > 0 else None

    @property
    def enabled(self) -> bool:
        return self.owner_id is not None

    async def prepare(self) -> None:
        if self.telegram.bot is None:
            await _retry(self.telegram.get_me, "Telegram bot identity")
        await _retry(
            lambda: self.telegram.configure_menu(TELEGRAM_COMMANDS),
            "Telegram command menu",
        )

    def _message_command(self, update: dict[str, Any]) -> str | None:
        message = update.get("message")
        if self.owner_id is None or not isinstance(message, dict):
            return None
        chat, sender, text = message.get("chat"), message.get("from"), message.get("text")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or type(chat.get("id")) is not int
            or chat["id"] != self.owner_id
            or not isinstance(sender, dict)
            or sender.get("is_bot") is not False
            or type(sender.get("id")) is not int
            or sender["id"] != self.owner_id
            or not isinstance(text, str)
            or not text.strip()
        ):
            return None
        text = text.strip()
        if text.startswith("/"):
            command, separator, username = text.split(maxsplit=1)[0].partition("@")
            if separator and (
                self.telegram.bot is None
                or username.lower() != self.telegram.bot.username.lower()
            ):
                return None
            return {
                "/start": "menu",
                "/status": "status",
                "/balance": "balance",
                "/usage": "balance",
                "/probe_now": "probe",
                "/probe": "probe",
                "/probing_schedule": "schedule",
                "/schedule": "schedule",
            }.get(command.lower())
        text = " ".join(text.lower().split())
        if text in SCHEDULE_CHOICES:
            return text
        return {
            "status": "status",
            "balance": "balance",
            "balance & usage": "balance",
            "usage": "balance",
            "probe now": "probe",
            "probing schedule": "schedule",
            "back": "menu",
        }.get(text)

    async def _reply(
        self, text: str, *, reply_markup: dict[str, Any] = TELEGRAM_MENU
    ) -> None:
        parts = telegram_message_parts(text)
        for index, part in enumerate(parts):
            async def send_part() -> None:
                try:
                    markup = reply_markup if index == len(parts) - 1 else None
                    await self.telegram.send(part, reply_markup=markup)
                except TransientClientError as exc:
                    self.prober.record_telegram_retry(exc)
                    raise

            await _retry(send_part, "Telegram command reply")

    async def handle_update(self, update: dict[str, Any]) -> None:
        command = self._message_command(update)
        if command == "status":
            if not self.prober.has_model_status:
                await self._reply("Checking every model listed on AgentRouter…")
            snapshot = await self.prober.status_snapshot()
            await self._reply(status_message(self.prober.settings, snapshot))
        elif command == "balance":
            await self._reply("Checking your AgentRouter balance…")
            try:
                usage = await self.prober.account_usage()
            except ClientError as exc:
                await self._reply(f"AgentRouter balance check failed: {exc}")
            else:
                await self._reply(account_usage_message(usage))
        elif command == "probe":
            await self._reply("Refreshing model-status and probing every listed model…")
            await self.prober.run_cycle()
            await self._reply(status_message(self.prober.settings, self.prober.state))
        elif command == "schedule":
            await self._reply(
                probing_schedule_message(
                    self.prober.poll_seconds, self.prober.settings.probe_concurrency
                ),
                reply_markup=TELEGRAM_SCHEDULE_MENU,
            )
        elif command is not None and command in SCHEDULE_CHOICES:
            try:
                self.prober.set_poll_seconds(SCHEDULE_CHOICES[command])
            except OSError:
                LOGGER.exception("Could not save the probing interval")
                await self._reply(
                    "The probing interval could not be saved. Try again.",
                    reply_markup=TELEGRAM_SCHEDULE_MENU,
                )
            else:
                await self._reply(
                    f"Automatic probes every {format_duration(self.prober.poll_seconds)}."
                )
        elif command == "menu":
            await self._reply(
                "Choose Status, Probe Now, Balance & Usage, or Probing Schedule below."
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        if stop_event.is_set():
            return
        await self.prepare()
        offset = None
        while not stop_event.is_set():
            updates = await _retry(
                lambda: self.telegram.get_updates(offset),
                "Telegram command listener",
            )
            for update in updates:
                if stop_event.is_set():
                    return
                await self.handle_update(update)
                offset = update["update_id"] + 1
            if not updates:
                await asyncio.sleep(0)


async def run_bot(prober: Prober, stop_event: asyncio.Event) -> None:
    commands = TelegramCommands(prober)
    if not commands.enabled:
        LOGGER.info(
            "Telegram commands require a private chat; continuing availability notifications"
        )
        await prober.run_forever(stop_event)
        return
    tasks = (
        asyncio.create_task(prober.run_forever(stop_event)),
        asyncio.create_task(commands.run_forever(stop_event)),
    )
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
