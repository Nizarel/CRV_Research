"""Translate SIGTERM/SIGINT into a stop event so loops drain within the grace period."""

from __future__ import annotations

import asyncio
import signal


def install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows event loops have no add_signal_handler.
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
