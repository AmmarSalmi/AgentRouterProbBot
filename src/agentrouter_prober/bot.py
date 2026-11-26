from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from typing import Any

from .app import Prober
from .clients import ClientError, TransientClientError
from .messages import (
    SCHEDULE_CHOICES,
    TELEGRAM_COMMANDS,
    TELEGRAM_CREDIT_ALERT_MENU,
    TELEGRAM_HELP,
    TELEGRAM_MENU,
    TELEGRAM_SCHEDULE_MENU,
    account_usage_message,
    credit_alert_confirmation,
    format_duration,
    probing_schedule_message,
    status_message,
    telegram_message_parts,
)
from .state import CreditAlertKind
from .telegram import _retry


LOGGER = logging.getLogger(__name__)
CREDIT_AMOUNT = re.compile(
    r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?|\.[0-9]{1,2}"
)


class TelegramCommands:
    def __init__(self, prober: Prober):
        self.prober = prober
        self.telegram = prober.telegram
        try:
            chat_id = int(self.telegram.chat_id or "")
        except ValueError:
            chat_id = 0
        self.owner_id = chat_id if chat_id > 0 else None
        self._credit_alert_input: CreditAlertKind | None = None

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

    def _message_text(self, update: dict[str, Any]) -> str | None:
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
        return text.strip()

    def _message_command(self, text: str) -> str | None:
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
                "/credit_alerts": "credit_alerts",
                "/help": "help",
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
            "credit alerts": "credit_alerts",
            "spending alert": "spending_alert",
            "low balance alert": "balance_alert",
            "help": "help",
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

    async def _save_credit_alert(self, kind: CreditAlertKind, text: str) -> None:
        amount_text = text.removeprefix("$").strip()
        try:
            if CREDIT_AMOUNT.fullmatch(amount_text) is None:
                raise ValueError("Enter a positive amount such as 2.50.")
            usage, alert = await self.prober.set_credit_alert(
                kind,
                Decimal(amount_text.replace(",", "")),
                dollar_sign=text.startswith("$"),
            )
        except (ClientError, ValueError) as exc:
            error = str(exc)
        except OSError:
            LOGGER.exception("Could not save the credit alert")
            error = "The credit alert could not be saved. Try again."
        else:
            self._credit_alert_input = None
            await self._reply(credit_alert_confirmation(kind, usage, alert.threshold_quota))
            return
        await self._reply(error, reply_markup=TELEGRAM_CREDIT_ALERT_MENU)

    async def handle_update(self, update: dict[str, Any]) -> None:
        text = self._message_text(update)
        if text is None:
            return
        command = self._message_command(text)
        if command is None:
            if self._credit_alert_input is not None and not text.startswith("/"):
                await self._save_credit_alert(self._credit_alert_input, text)
            return
        self._credit_alert_input = None

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
        elif command == "credit_alerts":
            await self._reply(
                "Choose a credit alert. Each alert sends once.",
                reply_markup=TELEGRAM_CREDIT_ALERT_MENU,
            )
        elif command in {"spending_alert", "balance_alert"}:
            if command == "spending_alert":
                self._credit_alert_input = "spending"
                prompt = "Enter how much more credit to spend before the alert."
            else:
                self._credit_alert_input = "balance"
                prompt = "Enter the balance to alert below."
            await self._reply(
                f"{prompt} Use the units shown in Balance & Usage.",
                reply_markup=TELEGRAM_CREDIT_ALERT_MENU,
            )
        elif command == "help":
            await self._reply(TELEGRAM_HELP)
        elif command == "menu":
            await self._reply("Choose an action below.")

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
