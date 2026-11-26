from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from importlib.resources import files
from typing import Any

from .app import Prober
from .clients import ClientError, ProtocolError, TelegramClient, TransientClientError
from .config import ACCOUNT_ID, DASHBOARD_TOKEN, MODEL_KEY, ConfigurationError
from .key_messages import (
    DAY_SECONDS,
    key_details,
    key_history_message,
    key_label,
    key_summary,
    key_troubleshooting,
)
from .messages import (
    SCHEDULE_CHOICES,
    TELEGRAM_COMMANDS,
    TELEGRAM_CREDIT_ALERT_MENU,
    TELEGRAM_HELP,
    TELEGRAM_MENU,
    TELEGRAM_SCHEDULE_MENU,
    account_usage_message,
    credit_alert_confirmation,
    format_account_amount,
    format_duration,
    probing_schedule_message,
    status_message,
    telegram_button_action,
    telegram_keyboard,
    telegram_message_parts,
)
from .setup import AgentRouterSetup
from .state import CreditAlertKind, KeyAlertKind, utc_now
from .telegram import _plain_message_text, _retry


LOGGER = logging.getLogger(__name__)
CREDIT_AMOUNT = re.compile(
    r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?|\.[0-9]{1,2}"
)
KEY_SELECTION = re.compile(r"Key #([0-9]{1,19})(?: · .*)?|🔑 .+ \(#([0-9]{1,19})\)", re.IGNORECASE)
KEY_PAGE_SIZE = 8
KEY_ALERT_ACTIONS: dict[str, KeyAlertKind] = {
    "key spending alert": "spending",
    "spend alert": "spending",
    "key low-quota alert": "balance",
    "low quota": "balance",
    "key expiry reminder": "expiry",
    "expiry reminder": "expiry",
}
KEY_HISTORY_PERIODS = {"last 24 hours": (1, "Last 24 hours"), "last 7 days": (7, "Last 7 days")}
KEY_ACTIONS = {
    "key details", "troubleshoot key", "key alerts", "clear key alerts",
    *KEY_HISTORY_PERIODS, *KEY_ALERT_ACTIONS,
}
WEEKLY_ACTIONS = {"weekly digest", "view weekly report", "enable weekly digest", "disable weekly digest"}

AGENTROUTER_CONSOLE_URL = "https://agentrouter.org/console"
SETUP_PROMPTS = {
    MODEL_KEY: "Send your AgentRouter API key for probing models (sk-…).",
    ACCOUNT_ID: (
        "Open Personal Settings in the AgentRouter console. "
        "Send the digits next to ID on your profile card.\n\n"
        f"{AGENTROUTER_CONSOLE_URL}"
    ),
    DASHBOARD_TOKEN: (
        "Open Personal Settings → Security Settings in the AgentRouter console. "
        "Select Generate Token under System Access Token, then send the raw token here.\n\n"
        f"{AGENTROUTER_CONSOLE_URL}"
    ),
}
SETUP_SCREENSHOTS = {
    ACCOUNT_ID: "agentrouter-account-id.png",
    DASHBOARD_TOKEN: "agentrouter-dashboard-token.png",
}
SETUP_MENU = telegram_keyboard(("Back",))


KEY_MENU = telegram_keyboard(
    ("Last 24 Hours", "Last 7 Days"), ("Key Alerts", "Troubleshoot Key"), ("API Keys", "Back")
)
KEY_ALERT_MENU = telegram_keyboard(
    ("Spend Alert", "Low Quota"),
    ("Expiry Reminder", "Clear Alerts"),
    ("Key Details", "API Keys"),
)


def _credit_amount(text: str) -> Decimal:
    amount = text.removeprefix("$").strip()
    if CREDIT_AMOUNT.fullmatch(amount) is None:
        raise ValueError("Enter a positive amount such as 2.50.")
    return Decimal(amount.replace(",", ""))


class TelegramCommands:
    def __init__(
        self,
        telegram: TelegramClient,
        prober: Prober | None = None,
        *,
        setup: AgentRouterSetup | None = None,
    ):
        self.telegram = telegram
        self._prober = prober
        self._setup = setup
        self._collecting_credentials = False
        self._ready = asyncio.Event()
        if prober is not None:
            self._ready.set()
        try:
            chat_id = int(self.telegram.chat_id or "")
        except ValueError:
            chat_id = 0
        self.owner_id = chat_id if chat_id > 0 else None
        self._credit_alert_input: CreditAlertKind | None = None
        self._key_alert_input: KeyAlertKind | None = None
        self._selected_key_id: int | None = None
        self._key_page = 0

    @property
    def enabled(self) -> bool:
        return self.owner_id is not None

    @property
    def prober(self) -> Prober:
        assert self._prober is not None
        return self._prober

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
        chat, sender = message.get("chat"), message.get("from")
        text = _plain_message_text(message)
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or type(chat.get("id")) is not int
            or chat["id"] != self.owner_id
            or not isinstance(sender, dict)
            or sender.get("is_bot") is not False
            or type(sender.get("id")) is not int
            or sender["id"] != self.owner_id
            or text is None
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
                "/api_keys": "api keys",
                "/weekly_digest": "weekly digest",
                "/help": "help",
            }.get(command.lower())
        text = telegram_button_action(text)
        if text in SCHEDULE_CHOICES:
            return text
        if text in KEY_ACTIONS or text in WEEKLY_ACTIONS or text in {"api keys", "next keys", "previous keys"}:
            return text
        return {
            "status": "status",
            "balance": "balance",
            "balance & usage": "balance",
            "usage": "balance",
            "probe now": "probe",
            "probing schedule": "schedule",
            "schedule": "schedule",
            "credit alerts": "credit_alerts",
            "spending alert": "spending_alert",
            "low balance alert": "balance_alert",
            "clear alerts": "clear key alerts",
            "view report": "view weekly report",
            "enable auto-send": "enable weekly digest",
            "disable auto-send": "disable weekly digest",
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
                    if self._prober is not None:
                        self._prober.record_telegram_retry(exc)
                    raise

            await _retry(send_part, "Telegram command reply")

    async def _activate(self) -> bool:
        assert self._setup is not None
        client = await self._setup.connect()
        if client is None:
            return False
        try:
            self._prober = Prober(client.settings, client, self.telegram)
        except BaseException:
            await client.close()
            raise
        self._ready.set()
        return True

    async def _advance_setup(self, note: str | None = None) -> None:
        assert self._setup is not None
        if self._setup.next_field is None:
            self._collecting_credentials = False
            await self._reply("⏳ Checking AgentRouter credentials…", reply_markup=SETUP_MENU)
            if await self._activate():
                await self._reply("✅ AgentRouter credentials verified and saved. Monitoring is active.")
                return
        field = self._setup.next_field
        self._collecting_credentials = field is not None
        details = note or self._setup.problem
        prompt = SETUP_PROMPTS[field] if field is not None else "Choose an AgentRouter feature to retry."
        lines = ["🔑 AgentRouter Setup", ""]
        if details:
            lines.extend((details, ""))
        lines.append(prompt)
        text = "\n".join(lines)
        screenshot = SETUP_SCREENSHOTS.get(field) if field is not None else None
        if screenshot is not None:
            try:
                photo = files("agentrouter_prober").joinpath("assets", screenshot).read_bytes()
                await _retry(
                    lambda: self.telegram.send_photo(
                        screenshot, photo, caption=text, reply_markup=SETUP_MENU
                    ),
                    "Telegram credential prompt photo",
                )
                return
            except (OSError, ProtocolError):
                LOGGER.warning("Could not send the AgentRouter setup screenshot for %s", field)
        await self._reply(text, reply_markup=SETUP_MENU if field is not None else TELEGRAM_MENU)

    async def _submit_credential(self, text: str, message_id: object) -> None:
        assert self._setup is not None
        if type(message_id) is int and message_id > 0:
            try:
                await self.telegram.delete_message(message_id)
            except ClientError:
                LOGGER.warning("Telegram could not delete a credential message")
        try:
            self._setup.submit(text)
        except ConfigurationError as exc:
            await self._advance_setup(str(exc))
        else:
            await self._advance_setup()

    async def _save_credit_alert(self, kind: CreditAlertKind, text: str) -> None:
        try:
            usage, alert = await self.prober.set_credit_alert(
                kind,
                _credit_amount(text),
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

    async def _show_api_keys(self, page: int) -> None:
        keys = await self.prober.agentrouter.fetch_api_keys()
        if not keys:
            await self._reply("🔑 API Keys\n\nNo keys listed.")
            return
        ordered = sorted(keys.values(), key=lambda key: (key.name.casefold(), key.id))
        pages = (len(ordered) + KEY_PAGE_SIZE - 1) // KEY_PAGE_SIZE
        self._key_page = min(max(page, 0), pages - 1)
        start = self._key_page * KEY_PAGE_SIZE
        entries = ordered[start:start + KEY_PAGE_SIZE]
        labels = [(f"🔑 {key_label(key, max_length=62)}",) for key in entries]
        navigation = tuple(
            label for label, show in (
                ("Previous Keys", self._key_page > 0), ("Next Keys", self._key_page < pages - 1),
            ) if show
        )
        if navigation:
            labels.append(navigation)
        labels.append(("Back",))
        display = await self.prober.agentrouter.quota_display_settings()
        try:
            usage = await self.prober.account_usage()
        except ClientError:
            balance = "Unavailable"
        else:
            balance = "Unlimited" if usage.quota < 0 else format_account_amount(usage, usage.quota)
        now = int(utc_now().timestamp())
        summaries = "\n\n".join(key_summary(key, display, now) for key in entries)
        page_number = f" · {self._key_page + 1}/{pages}" if pages > 1 else ""
        await self._reply(
            f"🔑 API Keys{page_number}\n💰 Shared balance · {balance}\n"
            f"Bars show % spent · [spent/left]\n\n{summaries}",
            reply_markup=telegram_keyboard(*labels),
        )

    async def _save_key_alert(self, key_id: int, kind: KeyAlertKind, text: str) -> None:
        try:
            confirmation = await self.prober.set_key_alert(
                key_id, kind, _credit_amount(text), dollar_sign=text.startswith("$")
            )
        except (ClientError, ValueError) as exc:
            await self._reply(str(exc), reply_markup=KEY_ALERT_MENU)
        except OSError:
            LOGGER.exception("Could not save the API key alert")
            await self._reply("The API key alert could not be saved. Try again.", reply_markup=KEY_ALERT_MENU)
        else:
            self._key_alert_input = None
            await self._reply(confirmation, reply_markup=KEY_ALERT_MENU)

    async def _show_key_action(self, command: str) -> None:
        key_id = self._selected_key_id
        if key_id is None:
            await self._show_api_keys(0)
            return
        # Keep clearing alerts possible when an old keyboard refers to a deleted key.
        if command == "clear key alerts":
            self.prober.clear_key_alerts(key_id)
            await self._reply(f"✅ Custom alerts cleared for key #{key_id}.", reply_markup=KEY_ALERT_MENU)
            return
        key = await self.prober.api_key(key_id)
        now = int(utc_now().timestamp())
        if command == "key details":
            display = await self.prober.agentrouter.quota_display_settings()
            await self._reply(key_details(key, display, now), reply_markup=KEY_MENU)
        elif command == "troubleshoot key":
            await self._reply(key_troubleshooting(key, self.prober.state, now), reply_markup=KEY_MENU)
        elif command in KEY_HISTORY_PERIODS:
            days, label = KEY_HISTORY_PERIODS[command]
            totals = await self.prober.agentrouter.fetch_usage_totals(now - days * DAY_SECONDS, now, key_id=key_id)
            display = await self.prober.agentrouter.quota_display_settings()
            await self._reply(key_history_message(key, totals, label, display), reply_markup=KEY_MENU)
        elif command == "key alerts":
            monitoring = self.prober.key_monitoring
            count = sum(id_ == key_id for id_, _ in monitoring.key_alerts) if monitoring else 0
            await self._reply(
                f"🔔 Key Alerts · {count} set\n{key_label(key)}\n\nEach alert sends once.",
                reply_markup=KEY_ALERT_MENU,
            )
        else:
            self._key_alert_input = KEY_ALERT_ACTIONS[command]
            prompt = {
                "spending": "Enter the additional spend to alert on.",
                "balance": "Enter the quota threshold to alert below.",
                "expiry": "Enter the number of days before expiry to send the reminder.",
            }[self._key_alert_input]
            if self._key_alert_input != "expiry":
                prompt += " Use the units shown for this key."
            await self._reply(prompt, reply_markup=KEY_ALERT_MENU)

    async def _show_weekly_digest(self, command: str) -> None:
        if command in {"enable weekly digest", "disable weekly digest"}:
            self.prober.set_weekly_digest(command == "enable weekly digest")
        monitoring = self.prober.key_monitoring
        enabled = monitoring is not None and monitoring.weekly_digest is not None
        menu = telegram_keyboard(
            ("View Report",), ("Disable Auto-send" if enabled else "Enable Auto-send", "Back")
        )
        if command == "view weekly report":
            await self._reply(await self.prober.weekly_usage(), reply_markup=menu)
        else:
            await self._reply(
                "📊 Weekly Digest\n\n"
                f"Auto-send · {'On' if enabled else 'Off'}\n"
                "Compares the last two completed UTC weeks.\n"
                "Delivery after Monday 00:00 UTC when enabled.",
                reply_markup=menu,
            )

    async def _handle_monitoring(self, command: str, selected: int | None = None) -> None:
        try:
            if selected is not None:
                self._selected_key_id = selected
                await self._show_key_action("key details")
            elif command in KEY_ACTIONS:
                await self._show_key_action(command)
            elif command in WEEKLY_ACTIONS:
                await self._show_weekly_digest(command)
            else:
                page = {"next keys": self._key_page + 1, "previous keys": self._key_page - 1}.get(command, 0)
                await self._show_api_keys(page)
        except (ClientError, ValueError) as exc:
            await self._reply(str(exc))
        except OSError:
            LOGGER.exception("Could not save key monitoring preferences")
            await self._reply("The monitoring preferences could not be saved. Try again.")

    async def handle_update(self, update: dict[str, Any]) -> None:
        text = self._message_text(update)
        if text is None:
            return
        command = self._message_command(text)
        selected = KEY_SELECTION.fullmatch(text)
        if command in {"help", "menu"}:
            self._credit_alert_input = None
            self._key_alert_input = None
            self._collecting_credentials = False
            await self._reply(TELEGRAM_HELP if command == "help" else "🤖 AgentRouter")
            return
        if self._prober is None:
            if command is not None or selected is not None:
                await self._advance_setup()
            elif self._collecting_credentials and not text.startswith("/"):
                await self._submit_credential(text, update["message"].get("message_id"))
            return
        if selected is not None:
            self._credit_alert_input = None
            self._key_alert_input = None
            await self._handle_monitoring("key details", int(selected[1] or selected[2]))
            return
        if command is None:
            if not text.startswith("/"):
                if self._key_alert_input is not None and self._selected_key_id is not None:
                    await self._save_key_alert(self._selected_key_id, self._key_alert_input, text)
                elif self._credit_alert_input is not None:
                    await self._save_credit_alert(self._credit_alert_input, text)
            return
        self._credit_alert_input = None
        self._key_alert_input = None

        if command == "status":
            if not self.prober.has_model_status:
                await self._reply("⏳ Checking listed models…")
            snapshot = await self.prober.status_snapshot()
            await self._reply(status_message(snapshot))
        elif command == "balance":
            await self._reply("⏳ Checking balance…")
            try:
                usage = await self.prober.account_usage()
            except ClientError as exc:
                await self._reply(f"AgentRouter balance check failed: {exc}")
            else:
                await self._reply(account_usage_message(usage))
        elif command == "probe":
            await self._reply("⏳ Probing every listed model…")
            await self.prober.run_cycle()
            await self._reply(status_message(self.prober.state))
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
                    f"✅ Automatic probes every {format_duration(self.prober.poll_seconds)}"
                )
        elif command == "credit_alerts":
            await self._reply(
                "🔔 Credit Alerts\n\nEach alert sends once.",
                reply_markup=TELEGRAM_CREDIT_ALERT_MENU,
            )
        elif command in {"spending_alert", "balance_alert"}:
            if command == "spending_alert":
                self._credit_alert_input = "spending"
                prompt = "Enter the additional spend to alert on."
            else:
                self._credit_alert_input = "balance"
                prompt = "Enter the balance to alert below."
            await self._reply(
                f"{prompt} Use the units shown in Balance.",
                reply_markup=TELEGRAM_CREDIT_ALERT_MENU,
            )
        else:
            await self._handle_monitoring(command)

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


    async def run(self, stop_event: asyncio.Event) -> None:
        async def monitor() -> None:
            await self._ready.wait()
            await self.prober.run_forever(stop_event)

        try:
            if self._prober is None:
                await self._activate()
                if self._setup and self._setup.problem:
                    LOGGER.warning("AgentRouter setup required. %s", self._setup.problem)

            if not self.enabled:
                if self._prober is not None:
                    LOGGER.info("Telegram commands require a private chat; continuing availability notifications")
                    await self.prober.run_forever(stop_event)
                else:
                    LOGGER.info("Set AgentRouter credentials in the environment or run setup in a private chat")
                    await stop_event.wait()
                return

            tasks = (
                asyncio.create_task(monitor()),
                asyncio.create_task(self.run_forever(stop_event)),
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
        finally:
            if self._prober is not None:
                await self._prober.agentrouter.close()
