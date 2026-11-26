from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from math import ceil
from time import monotonic

from .clients import (
    AccountUsage,
    AgentRouterClient,
    AuthenticationError,
    ClientError,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    ProtocolError,
    TelegramClient,
    TransientClientError,
)
from .config import Settings
from .messages import (
    availability_message,
    credit_alert_message,
    probe_key_balance_message,
    telegram_message_parts,
)
from .state import (
    CreditAlert,
    CreditAlertKind,
    PersistentState,
    ProbeKeyAlert,
    StateStore,
    format_datetime,
    parse_datetime,
    utc_now,
)


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class CycleResult:
    dashboard_ok: bool
    fatal_error: bool
    notifications_sent: int

    @property
    def status_ok(self) -> bool:
        return self.dashboard_ok


class Prober:
    def __init__(
        self,
        settings: Settings,
        agentrouter: AgentRouterClient,
        telegram: TelegramClient,
        store: StateStore | None = None,
    ):
        self.settings = settings
        self.agentrouter = agentrouter
        self.telegram = telegram
        self.store = store or StateStore(settings.state_path)
        self.state: PersistentState = self.store.load()
        key_identity = (
            f"{settings.agentrouter_base_url}\0{settings.agentrouter_user_id}"
            f"\0{settings.agentrouter_api_key.removeprefix('sk-')}"
        )
        self._probe_key_fingerprint = sha256(key_identity.encode()).hexdigest()
        self._cycle_lock = asyncio.Lock()
        self._poll_wakeup = asyncio.Event()
        self.telegram.send_retry_at = parse_datetime(self.state.telegram_retry_at)

    async def close(self) -> None:
        await self.agentrouter.close()
        await self.telegram.close()

    @property
    def has_model_status(self) -> bool:
        return bool(self.state.active_models) and all(
            self.state.model(model).last_observed_at is not None
            for model in self.state.active_models
        )

    @property
    def poll_seconds(self) -> int:
        return self.state.poll_seconds_override or self.settings.poll_seconds

    def set_poll_seconds(self, seconds: int | None) -> None:
        if seconds is not None and (type(seconds) is not int or seconds <= 0):
            raise ValueError("The probing interval must be a positive number of seconds")
        if seconds == self.state.poll_seconds_override:
            return
        self.store.save(replace(self.state, poll_seconds_override=seconds))
        self.state.poll_seconds_override = seconds
        self._poll_wakeup.set()

    async def account_usage(self) -> AccountUsage:
        return await self.agentrouter.fetch_account_usage()

    async def set_credit_alert(
        self, kind: CreditAlertKind, amount: Decimal, *, dollar_sign: bool = False
    ) -> tuple[AccountUsage, CreditAlert]:
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Enter a positive amount.")
        chat_id = self.telegram.chat_id
        if not chat_id or not chat_id.removeprefix("+").isdecimal() or int(chat_id) <= 0:
            raise ValueError("Credit alerts require a private Telegram chat.")

        usage = await self.account_usage()
        if kind == "balance" and usage.quota < 0:
            raise ValueError("A low-balance alert needs a finite account balance.")
        if usage.display_in_currency and usage.quota_per_unit > 0:
            threshold = ceil(amount * Decimal(str(usage.quota_per_unit)))
        else:
            if dollar_sign or amount != amount.to_integral_value():
                raise ValueError("This account uses quota units. Enter a whole number.")
            threshold = int(amount)

        if kind == "spending":
            threshold += usage.used_quota
        pending = None
        if kind == "balance" and usage.quota < threshold:
            pending = credit_alert_message(kind, usage, threshold)
        alert = CreditAlert(threshold, chat_id, self.settings.agentrouter_user_id, pending)
        alerts = {**self.state.credit_alerts, kind: alert}
        self.store.save(replace(self.state, credit_alerts=alerts))
        self.state.credit_alerts = alerts
        return usage, alert

    def _current_credit_alerts(self) -> dict[CreditAlertKind, CreditAlert]:
        return {
            kind: alert
            for kind, alert in self.state.credit_alerts.items()
            if alert.chat_id == self.telegram.chat_id
            and alert.account_id == self.settings.agentrouter_user_id
        }

    async def _check_credit_alerts(self) -> None:
        alerts = {
            kind: alert
            for kind, alert in self._current_credit_alerts().items()
            if alert.pending_message is None
        }
        if not alerts:
            return
        try:
            usage = await self.account_usage()
        except ClientError as exc:
            LOGGER.warning("AgentRouter credit alert check failed: %s", exc)
            return

        for kind, alert in alerts.items():
            if self.state.credit_alerts.get(kind) is not alert:
                continue
            threshold = alert.threshold_quota
            reached = (
                usage.used_quota >= threshold
                if kind == "spending"
                else 0 <= usage.quota < threshold
            )
            if reached:
                self.state.credit_alerts[kind] = replace(
                    alert, pending_message=credit_alert_message(kind, usage, threshold)
                )

    async def _check_probe_key_balance(self) -> None:
        chat_id = self.telegram.chat_id
        alert = self.state.probe_key_alert
        if alert and (
            alert.chat_id != chat_id or alert.key_fingerprint != self._probe_key_fingerprint
        ):
            alert = None
            self.state.probe_key_alert = None
        if not chat_id:
            return

        try:
            balance = await self.agentrouter.fetch_probe_key_balance()
        except ClientError as exc:
            LOGGER.warning("AgentRouter probe key balance check failed: %s", exc)
            return

        threshold = self.settings.probe_key_low_balance_usd
        if balance is None or balance >= threshold:
            self.state.probe_key_alert = None
        elif alert is None or alert.pending_message is not None:
            self.state.probe_key_alert = ProbeKeyAlert(
                self._probe_key_fingerprint,
                chat_id,
                probe_key_balance_message(balance, threshold),
            )

    async def status_snapshot(self) -> PersistentState:
        was_running = self._cycle_lock.locked()
        async with self._cycle_lock:
            if not was_running and not self.has_model_status:
                await self._run_and_save()
            return deepcopy(self.state)

    def record_telegram_retry(self, error: TransientClientError) -> None:
        if error.retry_after is None:
            return
        now = utc_now()
        deadline = self.telegram.send_retry_at
        if deadline is None or deadline <= now:
            deadline = now + timedelta(seconds=error.retry_after)
        self.telegram.send_retry_at = deadline
        self.state.telegram_retry_at = format_datetime(deadline)
        self.store.save(self.state)

    async def _deliver_pending_notifications(
        self, now: datetime
    ) -> tuple[int, bool]:
        pending_models = [
            model
            for model in self.state.active_models
            if self.state.model(model).notification_pending
        ]
        credit_notifications = [
            (kind, alert, alert.pending_message)
            for kind, alert in self._current_credit_alerts().items()
            if alert.pending_message is not None
        ]
        probe_key_alert = self.state.probe_key_alert
        probe_key_message = probe_key_alert.pending_message if probe_key_alert else None
        if not (pending_models or credit_notifications or probe_key_message):
            return 0, False
        retry_at = parse_datetime(self.state.telegram_retry_at)
        if retry_at is not None and utc_now() < retry_at:
            return 0, False
        self.state.telegram_retry_at = None
        sent = 0
        try:
            if pending_models:
                for part in telegram_message_parts(availability_message(self.state)):
                    await self.telegram.send(part)
                    sent += 1
                for model in pending_models:
                    record = self.state.model(model)
                    record.notification_pending = False
                    record.last_notified_at = format_datetime(now)
                LOGGER.info("Sent model availability list")
            for kind, alert, message in credit_notifications:
                if self.state.credit_alerts.get(kind) is not alert:
                    continue
                await self.telegram.send(message)
                sent += 1
                if self.state.credit_alerts.get(kind) is alert:
                    del self.state.credit_alerts[kind]
            if probe_key_alert and probe_key_message:
                await self.telegram.send(probe_key_message)
                sent += 1
                self.state.probe_key_alert = replace(probe_key_alert, pending_message=None)
        except TransientClientError as exc:
            self.state.last_error = str(exc)
            LOGGER.warning("Telegram notification will be retried: %s", exc)
            if exc.retry_after is not None:
                self.record_telegram_retry(exc)
            return sent, False
        except (AuthenticationError, ProtocolError) as exc:
            self.state.last_error = str(exc)
            LOGGER.error("Telegram notification failed: %s", exc)
            return sent, True
        return sent, False

    async def run_cycle(self, now: datetime | None = None) -> CycleResult:
        async with self._cycle_lock:
            return await self._run_and_save(now)

    async def _run_and_save(self, now: datetime | None = None) -> CycleResult:
        cycle_time = (now or utc_now()).astimezone(UTC)
        try:
            dashboard_ok, fatal = await self._probe_models(cycle_time)
            await self._check_credit_alerts()
            await self._check_probe_key_balance()
            sent, notification_fatal = await self._deliver_pending_notifications(cycle_time)
            return CycleResult(dashboard_ok, fatal or notification_fatal, sent)
        finally:
            self.store.save(self.state)

    async def _probe_one(
        self,
        observation: ModelObservation,
        cycle_time: datetime,
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, ModelProbe | Exception]:
        try:
            async with semaphore:
                probe = await self.agentrouter.probe_model(observation)
            return observation.name, probe
        except (AuthenticationError, ProtocolError, TransientClientError) as exc:
            return observation.name, exc

    async def _probe_models(self, cycle_time: datetime) -> tuple[bool, bool]:
        self.state.last_cycle_at = format_datetime(cycle_time)
        try:
            observations = await self.agentrouter.fetch_model_status()
        except TransientClientError as exc:
            self.state.last_error = str(exc)
            LOGGER.warning("AgentRouter model-status failed transiently: %s", exc)
            return False, False
        except (AuthenticationError, ProtocolError) as exc:
            self.state.last_error = str(exc)
            LOGGER.error("AgentRouter model-status failed: %s", exc)
            return False, True

        self.state.last_successful_status_at = format_datetime(cycle_time)
        self.state.last_error = None
        self.state.active_models = list(observations)
        for observation in observations.values():
            record = self.state.model(observation.name)
            record.dashboard_status = observation.status
            record.dashboard_tier = observation.current_tier
            record.apis = list(observation.apis)
            record.success_rate_24h = observation.success_rate_24h
            record.ttft_p90_ms_24h = observation.ttft_p90_ms_24h
            record.avg_latency_ms = observation.avg_latency_ms
            record.last_observed_at = format_datetime(cycle_time)

        semaphore = asyncio.Semaphore(self.settings.probe_concurrency)
        results = await asyncio.gather(
            *(
                self._probe_one(observation, cycle_time, semaphore)
                for observation in observations.values()
            )
        )

        fatal = False
        errors: list[str] = []
        for model, result in results:
            record = self.state.model(model)
            record.last_probed_at = format_datetime(cycle_time)
            if isinstance(result, Exception):
                record.probe_error = str(result)
                errors.append(str(result))
                if isinstance(result, (AuthenticationError, ProtocolError)):
                    fatal = True
                LOGGER.warning("Direct probe for %s failed: %s", model, result)
                continue

            previous = record.availability
            record.probe_api = result.api
            record.probe_latency_ms = result.latency_ms
            record.probe_reply = result.reply[:200] if result.reply else None
            record.probe_error = None
            if result.outcome is ProbeOutcome.AVAILABLE:
                record.availability = "available"
                record.last_confirmed_at = format_datetime(cycle_time)
                LOGGER.info(
                    "Direct probe confirmed %s via %s in %sms",
                    model,
                    result.api,
                    result.latency_ms,
                )
            else:
                record.availability = "unavailable"
                LOGGER.info("Direct probe reports %s unavailable", model)
            if previous != record.availability:
                record.notification_pending = True

        if errors:
            unique_errors = list(dict.fromkeys(errors))
            self.state.last_error = "; ".join(unique_errors[:3])
        return True, fatal

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        async def wake_on_stop() -> None:
            await stop_event.wait()
            self._poll_wakeup.set()

        stop_wakeup = asyncio.create_task(wake_on_stop())
        try:
            while not stop_event.is_set():
                started = monotonic()
                await self.run_cycle()
                earliest_next_cycle = monotonic() + 1
                while not stop_event.is_set():
                    self._poll_wakeup.clear()
                    next_cycle = max(started + self.poll_seconds, earliest_next_cycle)
                    delay = next_cycle - monotonic()
                    if delay <= 0:
                        break
                    LOGGER.info("Next full model probe in %.0f seconds", delay)
                    try:
                        async with asyncio.timeout(delay):
                            await self._poll_wakeup.wait()
                    except TimeoutError:
                        pass
        finally:
            stop_wakeup.cancel()
            await asyncio.gather(stop_wakeup, return_exceptions=True)
