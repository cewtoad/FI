"""Input sources for PTT: keyboard + HID gamepad (T6.1).

Abstracts the trigger so a keyboard key or an arbitrary gamepad button can drive
push-to-talk. Raw Input only (subscribe, never intercept/hook) - the project's
"passive, no injection" rule.

Binding format:
    kb:<vk>                        keyboard virtual-key, e.g. kb:0x6B
    hid:VID:PID:byte:mask          a byte/bit in a HID input report, e.g.
                                   hid:054C:0CE6:8:0x20

The keyboard source wraps the existing RawKeyTrigger. The HID source parses
RAWHID reports generically. Its report offsets are NOT hard-coded: use the
binding wizard / tools/probe_dualsense.py to discover them (STOP POINT #5).
By default the HID source is a no-op unless a binding is provided.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional

_log = logging.getLogger("f1_tr.input")

RIM_TYPEHID = 2


def parse_binding(binding: str):
    """Parse a binding string into a descriptor dict (or None if invalid)."""
    if not binding:
        return None
    parts = binding.strip().split(":")
    try:
        if parts[0] == "kb" and len(parts) == 2:
            return {"type": "kb", "vk": int(parts[1], 0)}
        if parts[0] == "hid" and len(parts) == 5:
            return {"type": "hid", "vid": int(parts[1], 16), "pid": int(parts[2], 16),
                    "byte": int(parts[3], 0), "mask": int(parts[4], 0)}
    except (ValueError, IndexError):
        return None
    return None


def format_binding(desc) -> str:
    if not desc:
        return ""
    if desc["type"] == "kb":
        return f"kb:0x{desc['vk']:02X}"
    return (f"hid:{desc['vid']:04X}:{desc['pid']:04X}:"
            f"{desc['byte']}:0x{desc['mask']:X}")


def hid_button_pressed(report: bytes, byte: int, mask: int) -> bool:
    """True when ``report[byte] & mask`` is set (pure, testable)."""
    if byte < 0 or byte >= len(report):
        return False
    return bool(report[byte] & mask)


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
        # NOTE: do NOT also fire on_tap here. voice_trigger.RawKeyTrigger already
        # emits press+release then its own tap, and a caller that passes both
        # on_release and on_tap would run its action twice (the "release
        # double-fired" bug fixed for voice_main). Tap is opt-in via the trigger.
        if self._pressed:
            self._pressed = False
            if self.on_release:
                self.on_release()


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
        # RawKeyTrigger already fired press+release; the base _emit_release
        # fired on_tap, so nothing else to do here.
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


class HidSource(InputSource):
    """Raw Input HID gamepad button source (generic binding)."""

    def __init__(self, binding: dict, on_press=None, on_release=None, on_tap=None):
        super().__init__(on_press, on_release, on_tap)
        self.binding = binding or {}
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._hwnd = None

    def start(self) -> None:
        if not self.binding:
            _log.info("HID source has no binding; disabled")
            return
        # NOTE (STOP POINT #5): the HID message loop mirrors the keyboard one
        # but additionally parses RAWHID reports. It is left unstarted by
        # default until the DualSense report offsets are confirmed on hardware.
        raise NotImplementedError(
            "HID source not enabled pending DualSense probe (see PLAN stop point #5)")

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            try:
                from voice_trigger import user32, WM_DESTROY
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass


def make_source(binding: str, on_press=None, on_release=None, on_tap=None,
                enable_hid: bool = False) -> Optional[InputSource]:
    """Build an input source from a binding string.

    Keyboard bindings always work. HID bindings return None unless
    ``enable_hid`` (the probe gate) is set.
    """
    desc = parse_binding(binding)
    if desc is None:
        return None
    if desc["type"] == "kb":
        return KeyboardSource(desc["vk"], on_press, on_release, on_tap)
    if desc["type"] == "hid":
        if not enable_hid:
            _log.info("HID binding present but disabled until probe is confirmed")
            return None
        return HidSource(desc, on_press, on_release, on_tap)
    return None
