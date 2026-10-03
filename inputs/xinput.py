"""Xbox controller PTT source via the XInput API (ctypes, zero deps).

XInput is what Xbox pads are designed for on Windows: the API hands over
NAMED button bits (A/B/X/Y/LB/RB/dpad/stick-clicks/Start/Back) plus analog
triggers, identical over USB / Bluetooth / the wireless adapter - no report
layout reverse-engineering needed. DualSense pads are NOT exposed as XInput
on stock Windows: those stay on the Raw Input HID path (inputs/hid.py).

xinput1_4.dll ships with Windows 8+; xinput1_3 is the fallback. Poll-based
(60 Hz): edge detection/debounce is the base-class press latch. Read-only
polling never conflicts with the game, which polls the same API.

Bindings: ``xi:A`` ``xi:RB`` ``xi:dup`` ... and ``xi:lt`` / ``xi:rt``
(triggers bind as a >=25% threshold). The Xbox guide button needs the
undocumented XInputGetStateEx and is deliberately not bindable.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional

from inputs.base import InputSource

_log = logging.getLogger("f1_tr.input")

_BUTTON_BITS = {
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
    "lb": 0x0100, "rb": 0x0200,
    "start": 0x0010, "back": 0x0020,
    "ls": 0x0040, "rs": 0x0080,
    "dup": 0x0001, "ddown": 0x0002, "dleft": 0x0004, "dright": 0x0008,
}
_TRIGGER_BTNS = ("lt", "rt")
_TRIGGER_THRESHOLD = 64   # 25% of 255
POLL_HZ = 60
USER_INDEXES = (0, 1, 2, 3)   # any connected XInput pad may trigger


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD),
                ("bLeftTrigger", wintypes.BYTE),
                ("bRightTrigger", wintypes.BYTE),
                ("sLX", ctypes.c_short), ("sLY", ctypes.c_short),
                ("sRX", ctypes.c_short), ("sRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


_dll_cache: Optional[ctypes.CDLL] = None


def _load_xinput():
    """Load xinput1_4 (Win8+, OS built-in), falling back to xinput1_3.
    Cached; returns None when neither is present (non-Windows / stripped OS)."""
    global _dll_cache
    if _dll_cache is not None:
        return _dll_cache or None
    for name in ("xinput1_4", "xinput1_3"):
        try:
            dll = ctypes.WinDLL(name)
            dll.XInputGetState.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(XINPUT_STATE)]
            dll.XInputGetState.restype = wintypes.DWORD
            _dll_cache = dll
            return dll
        except Exception:
            continue
    _dll_cache = False   # sentinel: tried and absent
    return None


def _read_state(dll, user_index: int):
    """(wButtons, lt, rt) for a pad slot, or None when nothing connected."""
    st = XINPUT_STATE()
    if dll.XInputGetState(user_index, ctypes.byref(st)) != 0:
        return None   # ERROR_DEVICE_NOT_CONNECTED etc.
    g = st.Gamepad
    return g.wButtons, g.bLeftTrigger, g.bRightTrigger


class XInputSource(InputSource):
    """PTT from an Xbox controller through the XInput API (``xi:<button>``).

    Polls all four XInput slots at POLL_HZ - whichever pad presses the bound
    button triggers. Buttons: a/b/x/y lb/rb start/back ls/rs dup/ddown/dleft/
    dright; lt/rt bind as a >=25% analog-trigger threshold.
    """

    name = "xinput"

    def __init__(self, binding: dict, on_press=None, on_release=None, on_tap=None):
        super().__init__(on_press, on_release, on_tap)
        self.binding = binding or {}
        self.button = str(binding.get("button", "")).lower()
        self._mask = _BUTTON_BITS.get(self.button)
        self._trigger = self.button in _TRIGGER_BTNS
        self.poll_hz = POLL_HZ
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------- internals

    @staticmethod
    def available() -> bool:
        return _load_xinput() is not None

    def _is_pressed(self) -> bool:
        dll = _load_xinput()
        if dll is None:
            return False
        for idx in USER_INDEXES:
            s = _read_state(dll, idx)
            if s is None:
                continue
            w, lt, rt = s
            if self._trigger:
                value = lt if self.button == "lt" else rt
                if value >= _TRIGGER_THRESHOLD:
                    return True
            elif w & self._mask:
                return True
        return False

    def _poll_once(self) -> None:
        """One poll step: the base-class latch turns state into press/release
        edges and debounces repeats."""
        if self._is_pressed():
            self._emit_press()
        else:
            self._emit_release()

    def _poll_loop(self) -> None:
        period = 1.0 / max(1, self.poll_hz)
        while self._running:
            self._poll_once()
            time.sleep(period)

    # ------------------------------------------------------------------ public

    def run_blocking(self) -> None:
        """Poll on the CURRENT thread (main thread in voice mode)."""
        self._running = True
        self._poll_loop()

    def start(self) -> None:
        if not self.binding:
            _log.info("XInput source has no binding; disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="f1tr-xinput", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False


def capture_xinput_binding(timeout_s: float = 6.0,
                           clock=None, sleep=None) -> Optional[dict]:
    """Wait for a pressed Xbox button and return an ``xi:`` binding dict.

    Blocking (run in a worker/HTTP thread). Buttons already held when the
    capture starts are part of the learned baseline (release first); a button
    that is released becomes capturable again. Bind-on-press - XInput state is
    authoritative named bits, so no hold-duration confirmation is needed.
    Windows + an XInput-capable pad required. clock/sleep are injectable for
    tests.
    """
    if sys.platform != "win32":
        return None
    dll = _load_xinput()
    if dll is None:
        return None
    clk = clock or time.monotonic
    slp = sleep or time.sleep
    t0 = clk()
    baseline: dict = {}   # user_index -> {button, ...} held at start

    def snapshot() -> dict:
        out = {}
        for idx in USER_INDEXES:
            s = _read_state(dll, idx)
            if s is None:
                continue
            w, lt, rt = s
            bits = {n for n, m in _BUTTON_BITS.items() if w & m}
            if lt >= _TRIGGER_THRESHOLD:
                bits.add("lt")
            if rt >= _TRIGGER_THRESHOLD:
                bits.add("rt")
            out[idx] = bits
        return out

    for idx, bits in snapshot().items():
        baseline[idx] = set(bits)
    while clk() - t0 < max(1.0, timeout_s):
        slp(0.03)
        for idx, bits in snapshot().items():
            base = baseline.setdefault(idx, set())
            for name in list(base):        # released -> re-press capturable
                if name not in bits:
                    base.discard(name)
            for name in bits:              # pressed and not in baseline
                if name not in base:
                    return {"binding": f"xi:{name}", "button": name}
    return None
