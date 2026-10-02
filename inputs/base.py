"""InputSource base class: press/release callbacks + press latching.

All PTT sources (keyboard, HID gamepad) emit press/release through
``_emit_press``/``_emit_release``, which latch on ``_pressed`` so repeated
"held" frames never double-fire (that latch IS the edge detection + debounce).
"""

from __future__ import annotations

from typing import Callable, Optional


class InputSource:
    """Base class: press/release/tap callbacks + start/stop."""

    def __init__(self, on_press=None, on_release=None, on_tap=None) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.on_tap = on_tap
        self._pressed = False

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    # a single source may report raw press/release, from which it derives tap
    def _emit_press(self) -> None:
        if not self._pressed:
            self._pressed = True
            if self.on_press:
                self.on_press()

    def _emit_release(self) -> None:
        # NOTE: do NOT also fire on_tap here. voice_trigger.RawKeyTrigger fires
        # tap as its own callback; a caller that passed both on_release and
        # on_tap used to run its action twice (the "release double-fired" bug
        # fixed for voice_main, which now consumes press/release only).
        if self._pressed:
            self._pressed = False
            if self.on_release:
                self.on_release()
