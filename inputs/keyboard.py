"""Keyboard PTT source: wraps voice_trigger.RawKeyTrigger + key capture.

Raw Input keyboard only (subscribe, never intercept/hook) - the project's
"passive, no injection" rule.
"""

from __future__ import annotations

import sys
from typing import Optional

from inputs.base import InputSource


class KeyboardSource(InputSource):
    """Thin wrapper over voice_trigger.RawKeyTrigger (keyboard raw input)."""

    def __init__(self, vk: int, on_press=None, on_release=None, on_tap=None):
        super().__init__(on_press, on_release, on_tap)
        from voice_trigger import RawKeyTrigger
        self.vk = vk
        self._trigger = RawKeyTrigger(on_tap=self._tap, vk=vk,
                                      on_press=self._press, on_release=self._release)

    def _press(self):
        self._emit_press()

    def _release(self):
        self._emit_release()

    def _tap(self):
        # RawKeyTrigger fires tap as its OWN callback (not via _emit_release,
        # which only ever fires on_release — the "release double-fired" bug).
        # PTT consumes press/release only, so tap is deliberately unused here.
        pass

    def start(self) -> None:
        self._trigger.start()

    def run_blocking(self) -> None:
        # Main-thread message loop is the recommended path.
        self._trigger.on_press = self._press
        self._trigger.on_release = self._release
        self._trigger.run_blocking()

    def stop(self) -> None:
        self._trigger.stop()


# Mouse buttons are never used for PTT; ignore them while capturing.
_MOUSE_VKS = {0x01, 0x02, 0x04, 0x05, 0x06}


def capture_keyboard_binding(timeout_s: float = 6.0, poll_hz: float = 60.0,
                             clock=None, sleep=None) -> Optional[dict]:
    """Wait for the next keyboard key press and return a ``kb:`` binding.

    Polls ``GetAsyncKeyState`` (passive read, no hook) until a key is pressed
    or the timeout elapses. Mouse buttons are ignored. Returns
    ``{"binding": "kb:0x6B", "vk": 107}`` or None on timeout. Windows only.
    """
    if sys.platform != "win32":
        return None
    import time as _time

    import ctypes
    user32 = ctypes.windll.user32
    clk = clock or _time.monotonic
    slp = sleep or _time.sleep
    period = 1.0 / max(1.0, poll_hz)

    def _down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    end = clk() + timeout_s
    # Wait until all keys are released (so the click that started capture, or a
    # key still held, is not mistaken for the target press).
    while clk() < end:
        if not any(_down(vk) for vk in range(1, 255) if vk not in _MOUSE_VKS):
            break
        slp(period)
    # Then wait for the first new press.
    while clk() < end:
        for vk in range(1, 255):
            if vk in _MOUSE_VKS:
                continue
            if _down(vk):
                return {"binding": f"kb:0x{vk:02X}", "vk": vk}
        slp(period)
    return None
