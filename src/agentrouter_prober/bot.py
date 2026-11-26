from __future__ import annotations

import asyncio
import logging
from typing import Any

from .app import Prober
from .clients import ClientError, TransientClientError
from .codex import CodexConfigError
from .messages import (
    TELEGRAM_COMMANDS,
    TELEGRAM_MENU,
    account_usage_message,
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
                "/set_model": "codex",
                "/models": "codex",
                "/probing_schedule": "schedule",
                "/schedule": "schedule",
            }.get(command.lower())
        return {
            "status": "status",
            "balance": "balance",
            "balance & usage": "balance",
            "usage": "balance",
            "probe now": "probe",
            "set codex model": "codex",
            "probing schedule": "schedule",
        }.get(" ".join(text.lower().split()))

    def _callback_model(self, update: dict[str, Any]) -> tuple[str, str] | None:
        query = update.get("callback_query")
        if self.owner_id is None or not isinstance(query, dict):
            return None
        sender, message = query.get("from"), query.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        data, query_id = query.get("data"), query.get("id")
        if (
            not isinstance(sender, dict)
            or sender.get("is_bot") is not False
            or sender.get("id") != self.owner_id
            or not isinstance(chat, dict)
            or chat.get("type") != "private"
            or chat.get("id") != self.owner_id
            or not isinstance(data, str)
            or not data.startswith("codex:")
            or not isinstance(query_id, str)
            or not query_id
        ):
            return None
        model = data.removeprefix("codex:")
        return query_id, model

    async def _reply(
        self, text: str, *, reply_markup: dict[str, Any] | None = TELEGRAM_MENU
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

    async def _codex_menu(self) -> None:
        if not self.prober.has_model_status:
            await self._reply("Loading the current AgentRouter model list…")
            await self.prober.status_snapshot()
        options = self.prober.codex_model_options
        try:
            current = (await self.prober.codex_config()).model
        except CodexConfigError as exc:
            await self._reply(str(exc))
            return
        if not options:
            await self._reply(
                "No model currently listed on model-status advertises the Responses API, "
                "so none can be selected for Codex."
            )
            return
        rows = []
        for model in options:
            callback = f"codex:{model}"
            if len(callback.encode("utf-8")) > 64:
                continue
            rows.append(
                [
                    {
                        "text": f"{'✓ ' if model == current else ''}{model}",
                        "callback_data": callback,
                    }
                ]
            )
        incompatible = [
            model
            for model in self.prober.state.active_models
            if model not in options
        ]
        text = "Choose the default model for Codex. Only Responses API models are shown."
        if current:
            text += f"\n\nCurrent: {current}"
        if incompatible:
            text += "\n\nNot Codex-compatible: " + ", ".join(incompatible)
        await self._reply(text, reply_markup={"inline_keyboard": rows})

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback = self._callback_model(update)
        if callback is not None:
            query_id, model = callback
            await self.telegram.answer_callback_query(query_id, "Updating Codex…")
            try:
                config = await self.prober.set_codex_model(model)
            except CodexConfigError as exc:
                await self._reply(f"Codex model was not changed: {exc}")
            else:
                await self._reply(
                    f"Codex default model is now {config.model}.\n"
                    "New Codex sessions will use it; restart an existing session to switch."
                )
            return

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
        elif command == "codex":
            await self._codex_menu()
        elif command == "schedule":
            await self._reply(probing_schedule_message(self.prober.settings))
        elif command == "menu":
            await self._reply(
                "Choose Status, Probe Now, Balance & Usage, Set Codex Model, "
                "or Probing Schedule below."
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
