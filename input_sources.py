"""PTT input sources - compatibility facade.

The implementation moved to the ``inputs/`` package (2026-10-03): base class
in ``inputs/base.py``, keyboard source in ``inputs/keyboard.py``, HID gamepad
source in ``inputs/hid.py``, binding strings in ``inputs/bindings.py``. This
module re-exports the public API so existing imports (voice_main, config_ui,
tests) keep working unchanged.
"""

from inputs import (  # noqa: F401
    CaptureScan,
    HidSource,
    InputSource,
    KeyboardSource,
    XInputSource,
    capture_hid_binding,
    capture_keyboard_binding,
    capture_xinput_binding,
    describe_binding,
    format_binding,
    hid_button_pressed,
    make_source,
    parse_binding,
    vid_pid_from_path,
)

__all__ = [
    "InputSource", "KeyboardSource", "HidSource", "XInputSource", "CaptureScan",
    "parse_binding", "format_binding", "describe_binding", "hid_button_pressed",
    "vid_pid_from_path", "capture_keyboard_binding", "capture_hid_binding",
    "capture_xinput_binding", "make_source",
]
