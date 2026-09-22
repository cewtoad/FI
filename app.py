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
from dataclasses import dataclass, field
from typing import Any, Optional

from engineer import Engineer
from receiver import DEFAULT_PORT, PACKETS_CONSUMED, TelemetryReceiver
from recorder import SessionRecorder
from state import TelemetryState
from summariser import Summariser


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
              recording: bool = True) -> App:
    logger = logger or logging.getLogger("f1_tr")
    state = TelemetryState(error_logger=logger)
    recorder = SessionRecorder() if recording else None

    def _on_packet(_packet: Any) -> None:
        if recorder is None:
            return
        try:
            recorder.record_state(state.snapshot())
        except Exception:
            pass

    receiver = TelemetryReceiver(
        state, port=port, bind_ip=bind_ip,
        interested=PACKETS_CONSUMED, logger=logger,
        on_packet=_on_packet)
    engineer = Engineer()
    return App(state=state, receiver=receiver, summariser=Summariser(),
               engineer=engineer, recorder=recorder)
