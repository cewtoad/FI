"""PTT input sources (keyboard / HID gamepad) - public API.

Split out of input_sources.py on 2026-10-03 (which remains as a compat
facade re-exporting everything defined here):

    inputs.bindings   binding strings (kb: / hid:), pure
    inputs.base       InputSource base class (press/release latching)
    inputs.keyboard   KeyboardSource + capture_keyboard_binding
    inputs.hid        HidSource + capture_hid_binding (DualSense-ready)
"""

from __future__ import annotations

import logging
from typing import Optional

from inputs.base import InputSource
from inputs.bindings import (describe_binding, format_binding, parse_binding)
from inputs.hid import (CaptureScan, HidSource, capture_hid_binding,
                        hid_button_pressed, vid_pid_from_path)
from inputs.keyboard import KeyboardSource, capture_keyboard_binding

_log = logging.getLogger("f1_tr.input")

__all__ = [
    "InputSource", "KeyboardSource", "HidSource", "CaptureScan",
    "parse_binding", "format_binding", "describe_binding", "hid_button_pressed",
    "vid_pid_from_path", "capture_keyboard_binding", "capture_hid_binding",
    "make_source",
]


def make_source(binding: str, on_press=None, on_release=None, on_tap=None,
                enable_hid: bool = True) -> Optional[InputSource]:
    """Build an input source from a binding string.

    Keyboard bindings always work. HID bindings are enabled by default since
    the DualSense layout was confirmed on hardware (2026-10-02); conservative
    callers can pass ``enable_hid=False`` to keep them gated.
    """
    desc = parse_binding(binding)
    if desc is None:
        return None
    if desc["type"] == "kb":
        return KeyboardSource(desc["vk"], on_press, on_release, on_tap)
    if desc["type"] in ("hid", "hat"):
        if not enable_hid:
            _log.info("HID binding present but disabled (enable_hid=False)")
            return None
        return HidSource(desc, on_press, on_release, on_tap)
    return None
