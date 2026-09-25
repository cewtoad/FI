"""Push-to-talk state machine (T6.2).

Pure logic, fully unit-testable: it takes press/release events and emits
actions (start_recording / stop_recording / none). No audio, no Windows APIs.

Modes:
  * hold   - press starts recording, release stops it.
  * toggle - tap (idle) starts recording; tap while recording stops it.

Quiet mode is NOT toggled in-game (B1 decision): a fast "tap start, tap stop"
was being misread as a double-tap and silently switched quiet instead of
stopping the recording. Quiet mode is now controlled from the config page only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

TAP_MAX_MS = 250
MIN_SAMPLES_HOLD_S = 0.3


@dataclass
class PttAction:
    kind: str            # "start_recording" | "stop_recording" | "none"
    at: float

    def __bool__(self) -> bool:
        return self.kind != "none"


class PTTController:
    def __init__(self, mode: str = "toggle",
                 double_tap_window_ms: int = 400,
                 clock=None) -> None:
        import time as _time
        self.mode = (mode or "toggle").lower()
        # Kept for config/signature compatibility; no longer used.
        self.double_tap_window_s = double_tap_window_ms / 1000.0
        self._clock = clock or _time.monotonic
        self._pressed = False
        self._press_at: Optional[float] = None
        self._recording = False

    # ------------------------------------------------------------- events

    def on_press(self) -> PttAction:
        now = self._clock()
        self._pressed = True
        self._press_at = now
        if self.mode == "hold":
            self._recording = True
            return PttAction("start_recording", now)
        return PttAction("none", now)

    def on_release(self) -> PttAction:
        now = self._clock()
        duration = (now - self._press_at) if self._press_at is not None else 0.0
        self._pressed = False
        self._press_at = None
        if self.mode == "hold":
            if self._recording:
                self._recording = False
                return PttAction("stop_recording", now)
            return PttAction("none", now)
        # toggle mode: recording -> stop (always, regardless of speed);
        # idle short tap -> start; idle long press -> nothing.
        if self._recording:
            self._recording = False
            return PttAction("stop_recording", now)
        if duration > TAP_MAX_MS / 1000.0:
            return PttAction("none", now)
        self._recording = True
        return PttAction("start_recording", now)

    # ------------------------------------------------------------- access

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def pressed(self) -> bool:
        return self._pressed

    def reset(self) -> None:
        self._pressed = False
        self._press_at = None
        self._recording = False
