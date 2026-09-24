"""Push-to-talk state machine (T6.2).

Pure logic, fully unit-testable: it takes press/release/tick events and emits
actions (start_recording / stop_recording / toggle_quiet / none). No audio, no
Windows APIs here.

Modes:
  * hold   - press starts recording, release stops it.
  * toggle - a short press toggles recording; a double-tap toggles quiet mode.

Double-tap detection: two taps whose press durations are each < TAP_MAX_MS and
whose start times are within PTT_DOUBLE_TAP_WINDOW_MS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

TAP_MAX_MS = 250
DEFAULT_DOUBLE_TAP_WINDOW_MS = 400
MIN_SAMPLES_HOLD_S = 0.3


@dataclass
class PttAction:
    kind: str            # "start_recording" | "stop_recording" | "toggle_quiet" | "none"
    at: float

    def __bool__(self) -> bool:
        return self.kind != "none"


class PTTController:
    def __init__(self, mode: str = "toggle",
                 double_tap_window_ms: int = DEFAULT_DOUBLE_TAP_WINDOW_MS,
                 clock=None) -> None:
        import time as _time
        self.mode = (mode or "toggle").lower()
        self.double_tap_window_s = double_tap_window_ms / 1000.0
        self._clock = clock or _time.monotonic
        self._pressed = False
        self._press_at: Optional[float] = None
        self._last_tap_at: Optional[float] = None
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
        # toggle mode: a short press is a tap; a double-tap is quiet toggle.
        if duration > TAP_MAX_MS / 1000.0:
            return PttAction("none", now)
        if (self._last_tap_at is not None
                and (now - self._last_tap_at) <= self.double_tap_window_s):
            self._last_tap_at = None
            self._recording = False
            return PttAction("toggle_quiet", now)
        self._last_tap_at = now
        self._recording = not self._recording
        return PttAction("start_recording" if self._recording else "stop_recording",
                         now)

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
        self._last_tap_at = None
        self._recording = False
