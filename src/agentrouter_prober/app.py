from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .clients import (
    AgentRouterClient,
    AuthenticationError,
    ModelObservation,
    ModelProbe,
    ProbeOutcome,
    ProtocolError,
    TelegramClient,
    TransientClientError,
)
from .codex import CodexConfig, CodexConfigError, CodexConfigManager
from .config import Settings
from .messages import availability_message, telegram_message_parts
from .state import (
    PersistentState,
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
        codex: CodexConfigManager | None = None,
    ):
        self.settings = settings
        self.agentrouter = agentrouter
        self.telegram = telegram
        self.store = store or StateStore(settings.state_path)
        self.state: PersistentState = self.store.load()
        self.codex = codex or CodexConfigManager(settings.codex_config_path)
        self._cycle_lock = asyncio.Lock()
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
    def codex_model_options(self) -> list[str]:
        return [
            model
            for model in self.state.active_models
            if "responses" in self.state.model(model).apis
        ]

    async def codex_config(self) -> CodexConfig:
        return await asyncio.to_thread(self.codex.read)

    async def set_codex_model(self, model: str) -> CodexConfig:
        if model not in self.state.active_models:
            raise CodexConfigError(
                f"{model} is no longer listed on AgentRouter model-status"
            )
        if "responses" not in self.state.model(model).apis:
            raise CodexConfigError(
                f"{model} is not Codex-compatible (Responses API is not advertised)"
            )
        return await asyncio.to_thread(self.codex.set_model, model)

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
        if not any(
            self.state.model(model).notification_pending
            for model in self.state.active_models
        ):
            return 0, False
        retry_at = parse_datetime(self.state.telegram_retry_at)
        if retry_at is not None and utc_now() < retry_at:
            return 0, False
        self.state.telegram_retry_at = None
        sent = 0
        try:
            for part in telegram_message_parts(availability_message(self.state)):
                await self.telegram.send(part)
                sent += 1
        except TransientClientError as exc:
            self.state.last_error = str(exc)
            LOGGER.warning("Telegram model list will be retried: %s", exc)
            if exc.retry_after is not None:
                self.record_telegram_retry(exc)
            return sent, False
        except (AuthenticationError, ProtocolError) as exc:
            self.state.last_error = str(exc)
            LOGGER.error("Telegram model list failed: %s", exc)
            return sent, True
        for model in self.state.active_models:
            record = self.state.model(model)
            record.notification_pending = False
            record.last_notified_at = format_datetime(now)
        LOGGER.info("Sent model availability list")
        return sent, False

    async def run_cycle(self, now: datetime | None = None) -> CycleResult:
        async with self._cycle_lock:
            return await self._run_and_save(now)

    async def _run_and_save(self, now: datetime | None = None) -> CycleResult:
        try:
            return await self._run_cycle(now)
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

    async def _run_cycle(self, now: datetime | None = None) -> CycleResult:
        cycle_time = (now or utc_now()).astimezone(UTC)
        self.state.last_cycle_at = format_datetime(cycle_time)
        try:
            observations = await self.agentrouter.fetch_model_status()
        except TransientClientError as exc:
            self.state.last_error = str(exc)
            LOGGER.warning("AgentRouter model-status failed transiently: %s", exc)
            sent, notification_fatal = await self._deliver_pending_notifications(cycle_time)
            return CycleResult(False, notification_fatal, sent)
        except (AuthenticationError, ProtocolError) as exc:
            self.state.last_error = str(exc)
            LOGGER.error("AgentRouter model-status failed: %s", exc)
            sent, notification_fatal = await self._deliver_pending_notifications(cycle_time)
            return CycleResult(False, True or notification_fatal, sent)

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
        sent, notification_fatal = await self._deliver_pending_notifications(cycle_time)
        return CycleResult(True, fatal or notification_fatal, sent)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            await self.run_cycle()
            elapsed = time.monotonic() - started
            delay = max(1, self.settings.poll_seconds - elapsed)
            LOGGER.info("Next full model probe in %.0f seconds", delay)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
