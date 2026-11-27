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
    ApiKey,
    AuthenticationError,
    ClientError,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    ProtocolError,
    QuotaDisplay,
    TelegramClient,
    TransientClientError,
)
from .config import Settings
from .key_messages import (
    DAY_SECONDS,
    WEEK_SECONDS,
    completed_week_end,
    key_alert_confirmation,
    key_alert_message,
    weekly_usage_message,
)
from .messages import (
    availability_message,
    credit_alert_message,
    probe_key_balance_message,
    telegram_message_parts,
)
from .state import (
    CreditAlert,
    CreditAlertKind,
    KeyAlert,
    KeyAlertKind,
    KeyMonitoring,
    ManualProbeState,
    PersistentState,
    ProbeKeyAlert,
    StateStore,
    WeeklyDigest,
    format_datetime,
    parse_datetime,
    utc_now,
)


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def _amount_in_quota(amount: Decimal, display: QuotaDisplay, dollar_sign: bool) -> int:
    quota_per_unit, display_in_currency = display
    if display_in_currency and quota_per_unit > 0:
        return ceil(amount * Decimal(str(quota_per_unit)))
    if dollar_sign or amount != amount.to_integral_value():
        raise ValueError("This account uses quota units. Enter a whole number.")
    return int(amount)


def _key_alert_due(key: ApiKey, alert: KeyAlert, now: int) -> bool:
    if alert.kind == "spending":
        return key.used_quota >= alert.threshold
    if alert.kind == "balance":
        return key.remaining_quota is not None and key.remaining_quota < alert.threshold
    return 0 < key.expires_at <= now + alert.threshold * DAY_SECONDS


@dataclass(frozen=True)
class CycleResult:
    dashboard_ok: bool
    fatal_error: bool
    notifications_sent: int


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
        if settings.agentrouter_probe_key_id is not None:
            key_identity += f"\0{settings.agentrouter_probe_key_id}"
        self._probe_key_fingerprint = sha256(key_identity.encode()).hexdigest()
        self._cycle_task: asyncio.Task[CycleResult] | None = None
        manual_probe = self.state.manual_probe
        self._interrupted_probe_at = (
            parse_datetime(manual_probe.last_requested_at)
            if manual_probe is not None and manual_probe.pending_update_id is not None else None
        )
        self._poll_wakeup = asyncio.Event()
        self.telegram.send_retry_at = parse_datetime(self.state.telegram_retry_at)

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
        display = usage.quota_per_unit, usage.display_in_currency
        threshold = _amount_in_quota(amount, display, dollar_sign)

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

    def _monitoring_scope(self) -> str | None:
        chat_id = self.telegram.chat_id
        if not chat_id or not chat_id.removeprefix("+").isdecimal() or int(chat_id) <= 0:
            return None
        account = f"{self.settings.agentrouter_base_url}\0{self.settings.agentrouter_user_id}"
        return sha256(f"{account}\0{int(chat_id)}".encode()).hexdigest()

    @property
    def key_monitoring(self) -> KeyMonitoring | None:
        monitoring = self.state.key_monitoring
        return monitoring if monitoring and monitoring.scope == self._monitoring_scope() else None

    def _save_key_monitoring(self, monitoring: KeyMonitoring) -> None:
        self.store.save(replace(self.state, key_monitoring=monitoring))
        self.state.key_monitoring = monitoring

    async def api_key(self, key_id: int) -> ApiKey:
        keys = await self.agentrouter.fetch_api_keys()
        if key_id not in keys:
            raise ValueError("This API key is no longer listed. Open API Keys to choose another.")
        return keys[key_id]

    async def set_key_alert(
        self, key_id: int, kind: KeyAlertKind, amount: Decimal, *, dollar_sign: bool = False
    ) -> str:
        scope = self._monitoring_scope()
        if scope is None:
            raise ValueError("API key alerts require a private Telegram chat.")
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Enter a positive amount.")
        key = await self.api_key(key_id)
        display = await self.agentrouter.quota_display_settings()
        now = int(utc_now().timestamp())
        if kind == "expiry":
            if dollar_sign or amount != amount.to_integral_value():
                raise ValueError("Enter a whole number of days before expiry.")
            if key.expires_at <= now:
                raise ValueError("The key needs a future expiry date for a reminder.")
            threshold = int(amount)
        else:
            if kind == "balance" and key.remaining_quota is None:
                raise ValueError("This key has unlimited quota. Choose a spending alert instead.")
            threshold = _amount_in_quota(amount, display, dollar_sign)
            if kind == "spending":
                threshold += key.used_quota
        alert = KeyAlert(key_id, kind, threshold)
        if _key_alert_due(key, alert, now):
            alert = replace(alert, pending_message=key_alert_message(key, alert, display))
        monitoring = self.key_monitoring or KeyMonitoring(scope)
        alerts = {**monitoring.key_alerts, (key_id, kind): alert}
        self._save_key_monitoring(replace(monitoring, key_alerts=alerts))
        return key_alert_confirmation(key, alert, display)

    def clear_key_alerts(self, key_id: int) -> None:
        monitoring = self.key_monitoring
        if monitoring:
            alerts = {slot: alert for slot, alert in monitoring.key_alerts.items() if slot[0] != key_id}
            self._save_key_monitoring(replace(monitoring, key_alerts=alerts))

    def set_weekly_digest(self, enabled: bool) -> None:
        scope = self._monitoring_scope()
        if scope is None:
            raise ValueError("Weekly digests require a private Telegram chat.")
        monitoring = self.key_monitoring or KeyMonitoring(scope)
        if enabled == (monitoring.weekly_digest is not None):
            return
        digest = WeeklyDigest(completed_week_end(utc_now())) if enabled else None
        self._save_key_monitoring(replace(monitoring, weekly_digest=digest))

    async def weekly_usage(self, end: int | None = None) -> str:
        week_end = completed_week_end(utc_now()) if end is None else end
        start = week_end - WEEK_SECONDS
        current = await self.agentrouter.fetch_usage_totals(start, week_end)
        previous = await self.agentrouter.fetch_usage_totals(start - WEEK_SECONDS, start)
        display = await self.agentrouter.quota_display_settings()
        return weekly_usage_message(week_end, current, previous, display)

    async def _check_weekly_digest(self, now: datetime) -> None:
        monitoring = self.key_monitoring
        digest = monitoring.weekly_digest if monitoring else None
        week_end = completed_week_end(now)
        if not digest or digest.pending_message or week_end <= digest.week_end:
            return
        try:
            message = await self.weekly_usage(week_end)
        except ClientError as exc:
            LOGGER.warning("AgentRouter weekly digest check failed: %s", exc)
            return
        monitoring = self.key_monitoring
        if monitoring and monitoring.weekly_digest is digest:
            self.state.key_monitoring = replace(
                monitoring, weekly_digest=WeeklyDigest(week_end, message)
            )

    async def _check_key_alerts(self, now: datetime) -> None:
        chat_id = self.telegram.chat_id
        alert = self.state.probe_key_alert
        if alert and (
            alert.chat_id != chat_id or alert.key_fingerprint != self._probe_key_fingerprint
        ):
            alert = None
            self.state.probe_key_alert = None
        if not chat_id:
            return

        monitoring = self.key_monitoring
        alerts = list(monitoring.key_alerts.items()) if monitoring else []
        try:
            keys = await self.agentrouter.fetch_api_keys()
        except ClientError as exc:
            LOGGER.warning("AgentRouter API key check failed: %s", exc)
            return

        probe_key = next((key for key in keys.values() if key.is_probe_key), None)
        remaining = probe_key.remaining_quota if probe_key else None
        if probe_key is None:
            LOGGER.warning("Configured probe key was not found in the dashboard key list")
        elif remaining is None:
            self.state.probe_key_alert = None
        if remaining is None and not alerts:
            return
        display = await self.agentrouter.quota_display_settings()
        if remaining is not None:
            balance = Decimal(remaining) / Decimal(str(display[0]))
            threshold = self.settings.probe_key_low_balance_usd
            if balance >= threshold:
                self.state.probe_key_alert = None
            elif alert is None or alert.pending_message is not None:
                self.state.probe_key_alert = ProbeKeyAlert(
                    self._probe_key_fingerprint, chat_id,
                    probe_key_balance_message(remaining, threshold, display),
                )
        monitoring = self.key_monitoring
        if monitoring:
            for slot, key_alert in alerts:
                if monitoring.key_alerts.get(slot) is not key_alert or key_alert.pending_message:
                    continue
                key = keys.get(key_alert.key_id)
                if key and _key_alert_due(key, key_alert, int(now.timestamp())):
                    monitoring.key_alerts[slot] = replace(
                        key_alert, pending_message=key_alert_message(key, key_alert, display)
                    )

    async def status_snapshot(self) -> PersistentState:
        if self.cycle_running or (not self.has_model_status and self._interrupted_probe_at is None):
            await self.run_cycle()
        return deepcopy(self.state)

    def save_manual_probe(self, request: ManualProbeState) -> None:
        self.store.save(replace(self.state, manual_probe=request))
        self.state.manual_probe = request

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
        monitoring = self.key_monitoring
        key_notifications = (
            [(slot, alert) for slot, alert in monitoring.key_alerts.items() if alert.pending_message]
            if monitoring else []
        )
        digest = monitoring.weekly_digest if monitoring else None
        digest_message = digest.pending_message if digest else None
        if not (pending_models or credit_notifications or probe_key_message or key_notifications or digest_message):
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
            for slot, alert in key_notifications:
                monitoring = self.key_monitoring
                if not monitoring or monitoring.key_alerts.get(slot) is not alert or not alert.pending_message:
                    continue
                await self.telegram.send(alert.pending_message)
                sent += 1
                monitoring = self.key_monitoring
                if monitoring and monitoring.key_alerts.get(slot) is alert:
                    del monitoring.key_alerts[slot]
            monitoring = self.key_monitoring
            if digest and digest_message and monitoring and monitoring.weekly_digest is digest:
                await self.telegram.send(digest_message)
                sent += 1
                monitoring = self.key_monitoring
                if monitoring and monitoring.weekly_digest is digest:
                    self.state.key_monitoring = replace(
                        monitoring, weekly_digest=replace(digest, pending_message=None)
                    )
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

    @property
    def cycle_running(self) -> bool:
        return self._cycle_task is not None and not self._cycle_task.done()

    def start_cycle(self, now: datetime | None = None) -> tuple[asyncio.Task[CycleResult], bool]:
        current = self._cycle_task
        if current is not None and not current.done():
            return current, False
        self._interrupted_probe_at = None
        self._cycle_task = asyncio.create_task(self._run_and_save(now))
        return self._cycle_task, True

    async def run_cycle(self, now: datetime | None = None) -> CycleResult:
        cycle, started = self.start_cycle(now)
        return await (cycle if started else asyncio.shield(cycle))

    async def cancel_cycle(self) -> None:
        if self._cycle_task is not None:
            if not self._cycle_task.done():
                self._cycle_task.cancel()
            await asyncio.gather(self._cycle_task, return_exceptions=True)

    async def _run_and_save(self, now: datetime | None = None) -> CycleResult:
        cycle_time = (now or utc_now()).astimezone(UTC)
        try:
            dashboard_ok, fatal = await self._probe_models(cycle_time)
            await self._check_credit_alerts()
            await self._check_key_alerts(cycle_time)
            await self._check_weekly_digest(cycle_time)
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
        started = None
        earliest_next_cycle = monotonic()
        if self._interrupted_probe_at is not None:
            elapsed = max(0, (utc_now() - self._interrupted_probe_at).total_seconds())
            started = earliest_next_cycle - elapsed
        try:
            while not stop_event.is_set():
                while started is not None and not stop_event.is_set():
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
                if stop_event.is_set():
                    break
                started = monotonic()
                await self.run_cycle()
                earliest_next_cycle = monotonic() + 1
        finally:
            stop_wakeup.cancel()
            await asyncio.gather(stop_wakeup, return_exceptions=True)
