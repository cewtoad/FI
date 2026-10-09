"""HID gamepad PTT source: Raw Input subscribe + generic byte/bit bindings.

The binding indexes the RAW report buffer as Windows delivers it (report ID
at byte 0 for numbered reports). DualSense 054C:0CE6 over USB, measured on
hardware 2026-10-02: input report 0x01 is 64 bytes; byte 8 = dpad (hat, low
nibble) + face buttons, byte 9 = L1/R1/L2/R2/Create/Options/L3/R3, byte 10 =
PS/touchpad/mute. So PTT on R1 = hid:054C:0CE6:9:0x02.

Raw Input only (RIDEV_INPUTSINK on a message-only window) - subscribe, never
inject/hook. Bluetooth DualSense uses a different report layout (first
version targets USB).
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from inputs.base import InputSource

_log = logging.getLogger("f1_tr.input")

RIM_TYPEHID = 2

if sys.platform == "win32":  # HID plumbing (mirrors voice_trigger.RawKeyTrigger)
    WM_INPUT = 0x00FF
    WM_DESTROY = 0x0002
    RID_INPUT = 0x10000003
    RIDEV_INPUTSINK = 0x00000100
    RIDI_DEVICENAME = 0x20000007

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Set argtypes so 64-bit handles/params don't overflow.
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                              ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.UINT)]
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE,
                                       ctypes.c_void_p]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                 wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", wintypes.INT), ("cbWndExtra", wintypes.INT),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

    class RAWHID(ctypes.Structure):
        _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD)]


def hid_button_pressed(report: bytes, byte: int, mask: int) -> bool:
    """True when ``report[byte] & mask`` is set (pure, testable)."""
    if byte < 0 or byte >= len(report):
        return False
    return bool(report[byte] & mask)


_VID_PID_RE = re.compile(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", re.IGNORECASE)


def vid_pid_from_path(path: str) -> Optional[tuple]:
    """Parse (vid, pid) from a Raw Input device path (pure, testable)."""
    m = _VID_PID_RE.search(path or "")
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)


def read_device_name(hdevice) -> str:
    """Raw Input device interface path for a WM_INPUT hDevice.

    NOTE: the name lookup must pass a pre-sized buffer in ONE call - the
    two-call NULL-buffer pattern does not work for RIDI_DEVICENAME (returns
    nothing useful; every device would silently look unknown).
    """
    if sys.platform != "win32":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.UINT(512)
        n = user32.GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME,
                                          buf, ctypes.byref(size))
        if n and n != 0xFFFFFFFF:
            return buf.value or ""
    except Exception:
        pass
    return ""


def _make_hid_window(wndproc, class_name: str):
    """Message-only window + ONE-call Gamepad/Joystick raw-input registration.

    RegisterRawInputDevices REPLACES the whole registration on every call, so
    per-usage calls would silently drop everything but the last (the probe
    tool's old bug). Gamepad (0x01/0x05) covers the DualSense; Joystick
    (0x01/04) covers wheels. Returns (hwnd, wndproc_keepalive).
    """
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = class_name
    user32.RegisterClassW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, class_name, "f1tr hid", 0,
                                  0, 0, 0, 0, wintypes.HWND(-3), None,
                                  hinst, None)
    if not hwnd:
        raise RuntimeError(f"CreateWindowExW failed: {kernel32.GetLastError()}")

    arr = (RAWINPUTDEVICE * 2)()
    for i, usage in enumerate((0x05, 0x04)):
        arr[i].usUsagePage = 0x01
        arr[i].usUsage = usage
        arr[i].dwFlags = RIDEV_INPUTSINK
        arr[i].hwndTarget = hwnd
    if not user32.RegisterRawInputDevices(arr, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        raise RuntimeError(
            f"RegisterRawInputDevices failed: {kernel32.GetLastError()}")
    return hwnd


class HidSource(InputSource):
    """Raw Input HID button source for gamepads/wheels (``hid:``/``hat:`` bindings).

    ``hid:VID:PID:byte:mask`` matches a bit in the RAW report buffer as
    Windows delivers it (report ID at byte 0 for numbered reports).
    ``hat:VID:PID:byte:value`` matches a hat/dpad nibble VALUE (0-7) — hats
    are value-coded, so a direction cannot be expressed as a bit mask.
    Reports from a different VID:PID (or unreadable device names) are ignored,
    so a second gamepad never triggers the bound PTT key.
    """

    def __init__(self, binding: dict, on_press=None, on_release=None, on_tap=None):
        super().__init__(on_press, on_release, on_tap)
        self.binding = binding or {}
        self.vid = int(binding.get("vid", 0))
        self.pid = int(binding.get("pid", 0))
        self.byte = int(binding.get("byte", 0))
        self.mask = int(binding.get("mask", 0))
        self._hat_value = (int(binding["value"])
                           if binding.get("type") == "hat" else None)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._hwnd = None
        self._wndproc = None
        self._ready = threading.Event()
        self._error = ""
        self._dev_paths = {}   # hDevice -> interface path (Raw Input lookup cache)
        self._err_count = 0

    # -------------------------------------------------- device identification

    @staticmethod
    def _name_matches(path: str, vid: int, pid: int) -> bool:
        """Pure helper: does a Raw Input device path belong to VID:PID?"""
        p = (path or "").lower()
        return bool(p) and f"vid_{vid:04x}" in p and f"pid_{pid:04x}" in p

    def _device_matches(self, hdr) -> bool:
        """True when the WM_INPUT came from the bound VID:PID (cached lookup)."""
        key = int(hdr.hDevice)
        if key not in self._dev_paths:
            path = read_device_name(hdr.hDevice)
            self._dev_paths[key] = path
            if not path:
                _log.warning("HidSource: cannot read device name for hDevice "
                             "%#x; its reports will be ignored", key)
        return self._name_matches(self._dev_paths[key], self.vid, self.pid)

    # ------------------------------------------------------- report handling

    def _feed_report(self, data: bytes) -> None:
        """Edge-detect the bound bit/hat value in one RAWHID report (pure).

        Reports shorter than the binding byte are ignored entirely: a device
        can interleave other report IDs and a short one must never fake a
        button release. Press/release latching (incl. debounce) is inherited
        from InputSource._emit_press/_emit_release.
        """
        if self.byte >= len(data):
            return
        if self._hat_value is not None:
            pressed = (data[self.byte] & 0x0F) == self._hat_value
        else:
            pressed = bool(data[self.byte] & self.mask)
        if pressed:
            self._emit_press()
        else:
            self._emit_release()

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                                 ctypes.byref(size),
                                                 ctypes.sizeof(RAWINPUTHEADER))
                    if got and got != 0xFFFFFFFF and \
                            size.value >= ctypes.sizeof(RAWINPUTHEADER):
                        hdr = ctypes.cast(
                            buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                        if hdr.dwType == RIM_TYPEHID and self._device_matches(hdr):
                            hid = ctypes.cast(
                                ctypes.addressof(buf)
                                + ctypes.sizeof(RAWINPUTHEADER),
                                ctypes.POINTER(RAWHID)).contents
                            off = (ctypes.sizeof(RAWINPUTHEADER)
                                   + ctypes.sizeof(RAWHID))
                            if hid.dwCount >= 1 and hid.dwSizeHid > 0:
                                self._feed_report(
                                    bytes(buf[off:off + hid.dwSizeHid]))
            except Exception:
                # One bad report must never kill the message loop.
                self._err_count += 1
                if self._err_count <= 3 or self._err_count % 100 == 0:
                    _log.warning("HidSource WM_INPUT parse failed (%d)",
                                 self._err_count, exc_info=True)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------- window / registration

    def _setup(self) -> None:
        """Create the message-only window and register Raw Input (loop thread)."""
        self._wndproc = WNDPROC(self._proc)
        # Unique class per instance: RegisterClassW does not overwrite, so a
        # fixed name would route a restarted source to the OLD (possibly
        # garbage-collected) wndproc.
        class_name = f"F1TRHidTrigger{id(self._wndproc):X}"
        self._hwnd = _make_hid_window(self._wndproc, class_name)

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while self._running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:   # WM_QUIT, or error (never busy-spin on -1)
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------ public

    def run_blocking(self) -> None:
        """Create the window and run the message loop on the CURRENT thread.

        Recommended path (mirrors RawKeyTrigger.run_blocking): call from the
        main thread so Ctrl+C raises between messages and callbacks stay off
        extra threads.
        """
        self._setup()
        self._running = True
        self._pump()

    def start(self) -> None:
        if not self.binding:
            _log.info("HID source has no binding; disabled")
            self._ready.set()
            return
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._ready.clear()

        def _run() -> None:
            try:
                self._setup()
            except Exception as e:  # noqa: BLE001 - report via _ready/_error
                self._error = repr(e)
                _log.error("HidSource setup failed: %r", e)
            finally:
                self._ready.set()
            if not self._error:
                self._pump()

        self._thread = threading.Thread(target=_run, name="f1tr-hid",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._error:
            raise RuntimeError(f"HID source setup failed: {self._error}")

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            # Destroy on the pump thread kills the raw-input delivery for this
            # window; fall back to posting if we are on a different thread.
            try:
                user32.DestroyWindow(self._hwnd)
            except Exception:
                try:
                    user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
                except Exception:
                    pass


class CaptureScan:
    """Pure state machine behind capture_hid_binding (unit-testable).

    Learns the at-rest bit floor during ``baseline_s`` (idle jitter, rolling
    counters, already-held buttons all end up in the floor), then waits for a
    0->1 transition that stays 1 for ``hold_s`` — and only ACCEPTS it after
    the button is released again (bind-on-release, like in-game rebinding).
    The release confirmation rejects long-dwell counter/timestamp bits, which
    can sit at 1 for many seconds without any human input (measured 2026-10-03:
    DualSense byte 13 bit 0 false-captured with hands off the pad).

    Per-device scan config: known DualSense (054C:0CE6) -> documented button
    bytes 8..10 only (byte 8 high nibble = face buttons as bits, byte 8 low
    nibble = dpad HAT captured as a VALUE: hats are value-coded, neutral 0x08
    has bits set and North clears them, so bit-scan cannot see directions);
    other gamepads -> generic bits 4..15 (button region; stick axes 1-4 and
    motion/touch payload 16+ excluded).
    """

    LO_BYTE = 4    # skip stick axes (generic fallback range)
    HI_BYTE = 15   # skip motion/touch payload (generic fallback range)
    # vid/pid -> config. DualSense: only the documented button bytes, tighter
    # timings (shoulder/triggers get tapped quickly; a 0.25s hold rejects real
    # taps) and byte10 masked to PS/touch/mute — bits 3-7 are vendor counter
    # territory, never buttons.
    KNOWN = {(0x054C, 0x0CE6): {"bits": ((8, 0xF0), (9, 0xFF), (10, 0x07)),
                                "hat": (8, 0x0F),
                                "baseline_s": 0.5, "hold_s": 0.12}}
    # VID-level config (any PID): Xbox pads (045E) walk the XInput driver stack,
    # report only on state change (sparse frames) and get tapped quickly.
    # Button layout not hardware-confirmed yet -> keep the generic scan range.
    # Common wheel brands (read-only PTT bind via the same generic HID path;
    # report layouts vary by firmware — probe then bind, never inject):
    #   046D Logitech, 044F Thrustmaster, 0EB7 Fanatec, 11FF / 0483 Guillemot
    #   / Thrustmaster variants, 294B Moza (when exposed as Joystick usage).
    KNOWN_VID = {
        0x045E: {"baseline_s": 0.5, "hold_s": 0.12},  # Xbox
        0x046D: {"baseline_s": 0.6, "hold_s": 0.15},  # Logitech (G29/G920/…)
        0x044F: {"baseline_s": 0.6, "hold_s": 0.15},  # Thrustmaster
        0x0EB7: {"baseline_s": 0.6, "hold_s": 0.15},  # Fanatec
        0x11FF: {"baseline_s": 0.6, "hold_s": 0.15},  # Guillemot / wheel hubs
        0x0483: {"baseline_s": 0.6, "hold_s": 0.15},  # ST / some wheel bases
        0x294B: {"baseline_s": 0.6, "hold_s": 0.15},  # Moza (when HID joystick)
    }
    # A held candidate that never releases within this window is a counter/
    # timestamp bit, not a button: blacklist it for this capture and keep
    # scanning (otherwise it would lock the whole capture until timeout).
    PENDING_TIMEOUT_S = 2.5

    def __init__(self, baseline_s: float = 0.8, hold_s: float = 0.25) -> None:
        self.baseline_s = baseline_s
        self.hold_s = hold_s
        self._t0: Optional[float] = None
        self._floor: dict = {}      # dev_key -> {(byte, mask), ...}
        self._cand: dict = {}       # dev_key -> {(byte, mask): first_seen}
        self._hat_floor: dict = {}  # dev_key -> set of nibble values seen at rest
        self._hat_cand: dict = {}   # dev_key -> {value: first_seen}
        self._pending: dict = {}    # dev_key -> ("bit",byte,mask) | ("hat",byte,value)
        self._pending_since: dict = {}  # dev_key -> now when pending started
        self.result: Optional[dict] = None

    def _config_for(self, dev_key: str) -> dict:
        generic = {"bits": tuple((b, 0xFF)
                                 for b in range(self.LO_BYTE, self.HI_BYTE + 1)),
                   "hat": None}
        vp = vid_pid_from_path(dev_key or "")
        if not vp:
            return generic
        vid, pid = vp
        cfg = self.KNOWN.get((vid, pid))
        if cfg is None:
            cfg = self.KNOWN_VID.get(vid)
        if cfg is None:
            return generic
        out = dict(cfg)
        out.setdefault("bits", generic["bits"])
        return out

    def feed(self, dev_key: str, data: bytes, now: float) -> Optional[dict]:
        """Consume one report; returns the binding dict once, on release."""
        if self.result is not None:
            return self.result
        if self._t0 is None:
            self._t0 = now
        elapsed = now - self._t0
        cfg = self._config_for(dev_key)
        bl = cfg.get("baseline_s", self.baseline_s)
        hs = cfg.get("hold_s", self.hold_s)
        floor = self._floor.setdefault(dev_key, set())
        cand = self._cand.setdefault(dev_key, {})

        # Bind-on-release: a held candidate is only accepted once it returns
        # to its at-rest state (bit cleared / hat back to a rested value).
        pend = self._pending.get(dev_key)
        if pend is not None:
            kind, b, x = pend
            if kind == "bit":
                held = b < len(data) and bool(data[b] & x)
            else:
                nib = data[b] & 0x0F if b < len(data) else None
                held = nib is not None \
                    and nib not in self._hat_floor.get(dev_key, set())
            if held:
                if now - self._pending_since.get(dev_key, now) \
                        > self.PENDING_TIMEOUT_S:
                    # Never released => a counter/timestamp bit, not a button.
                    # Blacklist it for this capture and keep scanning.
                    if kind == "bit":
                        floor.add((b, x))
                    else:
                        self._hat_floor.setdefault(dev_key, set()).add(x)
                    self._pending.pop(dev_key, None)
                    self._pending_since.pop(dev_key, None)
                else:
                    return None                # still held: keep waiting
            else:
                self._pending.pop(dev_key, None)
                self._pending_since.pop(dev_key, None)
                if kind == "bit":
                    self.result = {"dev": dev_key, "kind": "bit",
                                   "byte": b, "mask": x}
                else:
                    self.result = {"dev": dev_key, "kind": "hat",
                                   "byte": b, "value": x}
                return self.result

        hat = cfg.get("hat")
        if hat:
            hb, hmask = hat
            if hb < len(data):
                hf = self._hat_floor.setdefault(dev_key, set())
                hc = self._hat_cand.setdefault(dev_key, {})
                value = data[hb] & hmask
                if elapsed < bl:
                    hf.add(value)
                    hc.pop(value, None)
                elif value not in hf:
                    first = hc.setdefault(value, now)
                    if now - first >= hs:
                        self._pending[dev_key] = ("hat", hb, value)
                        self._pending_since[dev_key] = now
                        return None

        for b, bmask in cfg["bits"]:
            if b >= len(data):
                continue
            byte = data[b] & bmask
            for bit in range(8):
                mask = 1 << bit
                if not (bmask & mask):
                    continue
                key = (b, mask)
                if byte & mask:
                    if elapsed < bl:
                        floor.add(key)
                        cand.pop(key, None)
                    elif key not in floor:
                        first = cand.setdefault(key, now)
                        if now - first >= hs:
                            self._pending[dev_key] = ("bit", b, mask)
                            self._pending_since[dev_key] = now
                            cand.clear()
                            return None
                else:
                    cand.pop(key, None)
        return None


def capture_hid_binding(timeout_s: float = 8.0, baseline_s: float = 0.8,
                        hold_s: float = 0.25) -> Optional[dict]:
    """Wait for a gamepad/wheel button press; return a ``hid:`` binding dict.

    Blocking (run in a worker/HTTP thread, same contract as
    capture_keyboard_binding). Any Gamepad/Joystick-usage device is accepted;
    its VID:PID is parsed from the Raw Input device path, so the result is a
    complete binding like ``{"binding": "hid:054C:0CE6:9:0x02", "vid": 1356,
    "pid": 3318, "byte": 9, "mask": 2}``. A button held BEFORE the capture
    starts is part of the learned at-rest floor and cannot be captured -
    release everything first (same first-release-then-press contract as the
    keyboard capture). Windows only.
    """
    if sys.platform != "win32":
        return None

    scan = CaptureScan(baseline_s=baseline_s, hold_s=hold_s)
    state = {"win": None, "paths": {}}

    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                                 ctypes.byref(size),
                                                 ctypes.sizeof(RAWINPUTHEADER))
                    if got and got != 0xFFFFFFFF and \
                            size.value >= ctypes.sizeof(RAWINPUTHEADER):
                        hdr = ctypes.cast(
                            buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                        if hdr.dwType == RIM_TYPEHID:
                            key = int(hdr.hDevice)
                            if key not in state["paths"]:
                                state["paths"][key] = read_device_name(
                                    hdr.hDevice)
                            hid = ctypes.cast(
                                ctypes.addressof(buf)
                                + ctypes.sizeof(RAWINPUTHEADER),
                                ctypes.POINTER(RAWHID)).contents
                            off = (ctypes.sizeof(RAWINPUTHEADER)
                                   + ctypes.sizeof(RAWHID))
                            if hid.dwCount >= 1 and hid.dwSizeHid > 0:
                                win = scan.feed(state["paths"][key],
                                                bytes(buf[off:off + hid.dwSizeHid]),
                                                time.monotonic())
                                if win:
                                    state["win"] = win
            except Exception:
                pass
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(_proc)
    # Unique class per call: RegisterClassW does not overwrite an existing
    # class, so a fixed name would silently route every repeat capture to the
    # FIRST call's (by then garbage-collected) wndproc.
    class_name = f"F1TRHidCapture{id(wndproc):X}"
    try:
        hwnd = _make_hid_window(wndproc, class_name)
    except RuntimeError as e:
        _log.error("capture_hid_binding: %r", e)
        return None

    deadline = time.monotonic() + max(1.0, timeout_s) + baseline_s
    msg = wintypes.MSG()
    try:
        # PeekMessage pump (NOT GetMessageW+PostQuitMessage): a WM_QUIT left
        # in this thread's queue by any earlier teardown would otherwise make
        # the next capture on the same thread exit instantly (keep-alive HTTP
        # threads serve several requests).
        while time.monotonic() < deadline and state["win"] is None:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.002)
    finally:
        # Destroy synchronously (same thread): a lingering window would keep
        # receiving 250Hz WM_INPUT routed to the soon-freed callback.
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass

    win = state["win"]
    if not win:
        return None
    vp = vid_pid_from_path(win["dev"] or "")
    if not vp:
        return None
    vid, pid = vp
    if win["kind"] == "hat":
        b, v = win["byte"], win["value"]
        return {"binding": f"hat:{vid:04X}:{pid:04X}:{b}:{v}",
                "vid": vid, "pid": pid, "byte": b, "value": v}
    b, m = win["byte"], win["mask"]
    return {"binding": f"hid:{vid:04X}:{pid:04X}:{b}:0x{m:X}",
            "vid": vid, "pid": pid, "byte": b, "mask": m}
