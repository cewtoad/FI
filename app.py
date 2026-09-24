"""Composition root: build the wired application once, for every entry point.

run.py / webui.py / voice_main.py previously assembled receiver + state +
engineer + recorder + voice themselves, each slightly differently. This module
is the single place that knows how they fit together; entries become thin.

    build_app(port, bind_ip, recording=True) -> App

The receiver gets an ``on_packet`` hook that persists laps as soon as the
session goes live, so recording no longer depends on a browser polling
``/api/state``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from engineer import Engineer
from lib.f1_types import F1PacketType
from receiver import DEFAULT_PORT, PACKETS_CONSUMED, TelemetryReceiver
from recorder import SessionRecorder
from state import TelemetryState
from summariser import Summariser

import paths


@dataclass
class App:
    state: TelemetryState
    receiver: TelemetryReceiver
    summariser: Summariser
    engineer: Engineer
    recorder: Optional[SessionRecorder] = None
    extras: dict = field(default_factory=dict)

    def ctx(self) -> dict:
        """The context dict the HTTP layer expects."""
        return {
            "state": self.state,
            "receiver": self.receiver,
            "summariser": self.summariser,
            "engineer": self.engineer,
            "recorder": self.recorder,
            **self.extras,
        }


def build_app(port: int = DEFAULT_PORT, bind_ip: str = "127.0.0.1",
              logger: Optional[logging.Logger] = None,
              recording: bool = True,
              record_raw: bool = False,
              raw_path: Optional[str] = None) -> App:
    logger = logger or logging.getLogger("f1_tr")
    state = TelemetryState(error_logger=logger)
    recorder = SessionRecorder() if recording else None

    # T1.6: optional raw UDP recording. Off by default so a normal run does
    # not fill the disk; enable via --raw / F1TR_RECORD_RAW for replay capture.
    raw_sink = None
    raw_fh = None
    if record_raw:
        import time as _time
        from pathlib import Path as _Path

        from tools.udp_record import write_record
        raw_dir = _Path(raw_path) if raw_path else (paths.app_root() / "sessions")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"raw_{_time.strftime('%Y%m%d_%H%M%S')}.f1rec"
        raw_fh = open(raw_file, "wb")
        t0 = _time.monotonic()

        def raw_sink(payload: bytes) -> None:  # type: ignore[misc]
            write_record(raw_fh, int((_time.monotonic() - t0) * 1000), payload)
        logger.info("recording raw UDP packets to %s", raw_file)

    # Keep the receive hot path cheap: persist laps at most RECORD_HZ times a
    # second, plus immediately whenever a new lap completes (LAP_DATA).
    record_hz = 2.0
    last_record = [0.0]

    def _on_packet(packet: Any) -> None:
        if recorder is None:
            return
        now = time.monotonic()
        pkt_id = getattr(getattr(packet, "m_header", None), "m_packetId", None)
        due = (now - last_record[0]) >= (1.0 / record_hz)
        if not due and pkt_id != F1PacketType.LAP_DATA:
            return
        last_record[0] = now
        try:
            recorder.record_state(state.snapshot())
        except Exception:
            pass

    receiver = TelemetryReceiver(
        state, port=port, bind_ip=bind_ip,
        interested=PACKETS_CONSUMED, logger=logger,
        on_packet=_on_packet, raw_sink=raw_sink)
    engineer = Engineer()
    app = App(state=state, receiver=receiver, summariser=Summariser(),
              engineer=engineer, recorder=recorder)
    if raw_fh is not None:
        app.extras["raw_file"] = str(raw_file)
        app.extras["raw_fh"] = raw_fh
    return app
