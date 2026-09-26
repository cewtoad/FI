"""A small periodic dispatcher thread (T3.3).

The receiver thread must stay cheap; everything derived (race model, radio
director, config reload, debrief trigger) runs here at a low, fixed rate
(default 2 Hz). Every handler is isolated: its exception is caught and counted
so one bad consumer can never stop the tick.

Clock and sleep are injectable so tests drive ticks manually via ``run_once``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

Handler = Callable[[dict, float], None]


class Ticker:
    def __init__(self, rate_hz: float = 2.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 snapshot_provider: Optional[Callable[[], dict]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.rate_hz = rate_hz
        self._clock = clock
        self._sleep = sleep
        self._snapshot_provider = snapshot_provider
        self._log = logger or logging.getLogger("f1_tr.ticker")
        self._handlers: Dict[str, Handler] = {}
        self.errors: Dict[str, int] = {}
        self.ticks = 0
        self.last_beat_ms: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def add(self, handler: Handler, name: Optional[str] = None) -> None:
        key = name or getattr(handler, "__name__", f"handler{len(self._handlers)}")
        self._handlers[key] = handler

    def run_once(self) -> None:
        """Run every handler once against a fresh snapshot (tests use this)."""
        try:
            snapshot = self._snapshot_provider() if self._snapshot_provider else {}
        except Exception as e:  # noqa: BLE001 - an escaped error would kill the thread
            self.errors["__snapshot__"] = self.errors.get("__snapshot__", 0) + 1
            self._log.warning("ticker snapshot failed: %r", e)
            self.ticks += 1
            return
        now = self._clock()
        t0 = self._clock()
        for name, handler in list(self._handlers.items()):
            try:
                handler(snapshot, now)
            except Exception as e:  # noqa: BLE001 - isolate one bad consumer
                self.errors[name] = self.errors.get(name, 0) + 1
                self._log.warning("ticker handler %s failed: %r", name, e)
        self.ticks += 1
        self.last_beat_ms = (self._clock() - t0) * 1000.0
        if self.last_beat_ms > 200.0:
            self._log.warning("ticker beat took %.1fms", self.last_beat_ms)

    def _run(self) -> None:
        period = 1.0 / self.rate_hz if self.rate_hz > 0 else 0.5
        while not self._stop.is_set():
            self.run_once()
            self._sleep(period)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="f1tr-ticker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def stats(self) -> dict:
        # run_once (ticker thread) inserts error keys while HTTP readers copy
        # the dict; keys are a bounded set, so a bounded retry always converges.
        errors = {}
        for _ in range(3):
            try:
                errors = dict(self.errors)
                break
            except RuntimeError:
                continue
        return {"ticks": self.ticks, "errors": errors,
                "last_beat_ms": self.last_beat_ms}
