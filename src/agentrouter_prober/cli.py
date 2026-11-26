from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Coroutine
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app import Prober
from .bot import run_bot
from .browser_session import BrowserSessionError, ZenSessionLoader
from .clients import (
    AgentRouterClient,
    AuthenticationError,
    ProtocolError,
    TelegramClient,
    TransientClientError,
)
from .config import ConfigurationError, Settings, TelegramSettings, _discover_zen_profile, _url
from .state import health_is_fresh
from .telegram import resolve_telegram_chat


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor AgentRouter model availability and notify Telegram"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Run one polling cycle")
    modes.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a Telegram test message without contacting AgentRouter",
    )
    modes.add_argument(
        "--check-session",
        action="store_true",
        help="Check the local Zen session files without making network requests",
    )
    modes.add_argument(
        "--healthcheck",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = __import__("time").gmtime
    # HTTP transport logs include Telegram's credential-bearing request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def _shutdown_signals(stop_event: asyncio.Event):
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(signum, frame):
        loop.call_soon_threadsafe(stop_event.set)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, request_stop)
        except (OSError, ValueError):
            pass
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def _run_until_stopped(
    operation: Coroutine[Any, Any, int], stop_event: asyncio.Event
) -> int:
    if stop_event.is_set():
        operation.close()
        return 0
    work_task = asyncio.create_task(operation)
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (work_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        if work_task in done:
            return await work_task
        work_task.cancel()
        try:
            await work_task
        except asyncio.CancelledError:
            pass
        return 0
    finally:
        # Cancel requests and drain their cleanup before HTTP clients are closed.
        for task in (work_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(work_task, stop_task, return_exceptions=True)


async def _test_telegram() -> int:
    settings = TelegramSettings.from_env()
    client = TelegramClient(settings)
    stop_event = asyncio.Event()

    async def run() -> int:
        path = Path(os.getenv("STATE_PATH", "/data/state.json"))
        if not await resolve_telegram_chat(client, path, stop_event):
            return 0
        await client.send("AgentRouter prober Telegram test successful.")
        print("Telegram test message sent.")
        return 0

    try:
        with _shutdown_signals(stop_event):
            return await _run_until_stopped(run(), stop_event)
    finally:
        await client.close()


async def _run(settings: Settings, once: bool) -> int:
    telegram = TelegramClient(
        settings.telegram, timeout_seconds=settings.request_timeout_seconds
    )
    stop_event = asyncio.Event()

    async def run() -> int:
        if not await resolve_telegram_chat(telegram, settings.state_path, stop_event):
            return 0
        agentrouter = AgentRouterClient(settings)
        try:
            prober = Prober(settings, agentrouter, telegram)
            if once:
                result = await prober.run_cycle()
                return 2 if result.fatal_error or not result.dashboard_ok else 0
            await run_bot(prober, stop_event)
            return 0
        finally:
            await agentrouter.close()

    try:
        with _shutdown_signals(stop_event):
            return await _run_until_stopped(run(), stop_event)
    finally:
        await telegram.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging()
    try:
        if args.healthcheck:
            path = Path(os.getenv("STATE_PATH", "/data/state.json"))
            max_age = int(os.getenv("HEALTH_MAX_AGE_SECONDS", "3600"))
            return 0 if health_is_fresh(path, max_age) else 1
        if args.test_telegram:
            return asyncio.run(_test_telegram())
        if args.check_session:
            profile = _discover_zen_profile()
            if profile is None:
                raise ConfigurationError("ZEN_PROFILE_PATH is required to check the session files")
            try:
                ZenSessionLoader(profile, _url("AGENTROUTER_BASE_URL", "https://agentrouter.org")).load()
            except BrowserSessionError as exc:
                raise ConfigurationError(str(exc)) from exc
            print("AgentRouter session files are readable and contain a local login.")
            return 0
        return asyncio.run(_run(Settings.from_env(), args.once))
    except ConfigurationError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        return 2
    except (AuthenticationError, ProtocolError, TransientClientError) as exc:
        logging.getLogger(__name__).error("Request failed: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

