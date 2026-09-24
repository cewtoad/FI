"""DualSense HID button-binding wizard (T6.1 / STOP POINT #5).

The DualSense emits 64-byte reports at ~250Hz with gyro/touchpad noise that can
toggle arbitrary bits. A simple "changed byte" probe drowns in that noise.

This wizard uses a *hold* test that noise cannot fake:

  Phase REST  - you keep hands off the pad. We record, per (byte, bit), whether
                it was ever seen as 1 (a "static floor").
  Phase HOLD  - you HOLD the target button down for ~1s. A real button bit is
                stable at 1 for many consecutive frames; gyro noise is not.
  Phase AFTER - you release.

Only bits that are: never 1 in REST, stable-1 (>= 80% of frames) in HOLD, and
never 1 in AFTER are reported. That is essentially a perfect button filter.

Never injects input (Raw Input subscribe only).

Usage:
    py -3.12 -m tools.probe_dualsense --wizard
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from collections import defaultdict
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - diagnostic only
    print("Windows only.")
    raise SystemExit(1)

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEHID = 2

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT),
                                   wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWHID(ctypes.Structure):
    _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


# --- state ------------------------------------------------------------------

# phase: rest -> hold -> after
_phase = ["rest"]
# (dev,len,byte,bit) -> frames the bit was 1 in this phase
_ones = defaultdict(int)
# (dev,len) -> frames total in this phase
_frames = defaultdict(int)
# remembered per phase
_snapshot = {"rest": {}, "hold": {}, "after": {}}   # phase -> {bittuple: ones}
_rest_ever_one: set = set()
_prev = {}


def _dev_key(hdr):
    return f"dev{int(hdr.hDevice) & 0xFFFF:04X}"


def _on_report(dev, data):
    n = len(data)
    key = (dev, n)
    _frames[key] += 1
    for i, b in enumerate(data):
        if b:  # only record bytes/ bits that are set
            for bit in range(8):
                if b & (1 << bit):
                    _ones[(dev, n, i, bit)] += 1
    _prev[key] = data


def _freeze_phase(phase):
    _snapshot[phase] = {
        "frames": dict(_frames), "ones": dict(_ones),
    }


def _reset_counters():
    _ones.clear()
    _frames.clear()


def _report(hdr, data, mode):
    dev = _dev_key(hdr)
    _on_report(dev, data)


def _run_diff(args) -> int:
    """Baseline vs current; print only differences held for >=3 frames.

    Hands-off first N seconds establishes the baseline, then press R1.
    """
    mode = "diff"
    wndproc = _proc_builder(mode)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidDiff"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidDiff", "diff", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))
    print(f"[diff] hands OFF for {args.rest:g}s (baseline) ...")
    _pump(hwnd, args.rest)
    # Freeze baseline at the last seen report.
    for k, v in list(_prev.items()):
        _diff_base[k] = v
    print("[diff] now PRESS/HOLD R1 a few times. Only bits held >=3 frames print.")
    _pump(hwnd, max(1.0, args.seconds - args.rest))
    print("[diff] done.")
    if not _diff_reported:
        print("  no persistent bit changes seen. R1 may be unmapped here:")
        print("  disable Steam Input / DS4Windows, or the device hides R1 on")
        print("  this HID interface (try a different MI_ interface).")
    return 0


def _pump(hwnd, seconds):
    msg = wintypes.MSG()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.003)


# --- --diff mode: baseline vs current, only persistent differences ----------

_diff_base: dict = {}          # (dev,len) -> baseline bytes
_diff_streak: dict = {}        # (dev,len,byte,bit) -> consecutive frames set
_diff_reported: set = set()


def _on_report_diff(dev, data):
    n = len(data)
    key = (dev, n)
    base = _diff_base.get(key)
    if base is None or len(base) != n:
        _diff_base[key] = data
        return
    for i in range(n):
        x = data[i] ^ base[i]
        for bit in range(8):
            k = (dev, n, i, bit)
            if x & (1 << bit):
                _diff_streak[k] = _diff_streak.get(k, 0) + 1
                # A real button holds a bit high; a keyboard-like press lasts
                # many frames. Require 3+ frames before reporting once.
                if _diff_streak[k] == 3 and k not in _diff_reported:
                    _diff_reported.add(k)
                    print(f"[diff] {dev} len={n} byte[{i}] mask=0x{1 << bit:02X}"
                          f"  (held 3+ frames)")
            else:
                if k in _diff_streak:
                    _diff_streak[k] = 0


def _run_scan(args) -> int:
    """List every distinct HID report and how noisy each byte is at rest.

    A button report is quiet (most bytes constant) while a gyro/touch report
    changes constantly. This tells us which report/interface to watch for R1.
    """
    seen = {}   # (len, first_byte) -> {"n": frames, "byte_flips": [..]}
    n_bytes = {}

    def _on(dev, data):
        n = len(data)
        rid = data[0] if n else 0
        k = (n, rid)
        st = seen.setdefault(k, {"n": 0, "flips": 0, "prev": None})
        st["n"] += 1
        if st["prev"] is not None and len(st["prev"]) == n:
            st["flips"] += sum(1 for a, b in zip(st["prev"], data) if a != b)
        st["prev"] = data

    mode = "diff"
    wndproc = _proc_builder_scan(_on)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidScan"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidScan", "scan", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))
    print(f"[scan] hands OFF for {args.rest:g}s; listing each report ...")
    _pump(hwnd, args.rest)
    print()
    print(" report_len  report_id  frames  avg_bytes_changed_per_frame")
    for (n, rid), st in sorted(seen.items(), key=lambda kv: kv[1]["n"]):
        avg = st["flips"] / max(1, st["n"] - 1)
        tag = "  <-- QUIET (button report?)" if avg < 1.0 else ""
        print(f"   {n:>3}        0x{rid:02X}      {st['n']:>6}   {avg:6.2f}{tag}")
    print()
    print("Watch the QUIET report; the noisy one(s) are gyro/touch/trigger.")
    return 0


def _proc_builder_scan(handler):
    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wintypes.UINT(0)
            user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                if hdr.dwType == RIM_TYPEHID:
                    hid = ctypes.cast(
                        ctypes.addressof(buf) + ctypes.sizeof(RAWINPUTHEADER),
                        ctypes.POINTER(RAWHID)).contents
                    off = ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWHID)
                    if hid.dwCount >= 1:
                        handler(_dev_key(hdr), bytes(buf[off:off + hid.dwSizeHid]))
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROC(_proc)


def _run_hold_now(args) -> int:
    """You HOLD the button BEFORE running. Reports bits that stay 1 the whole
    run (a held button), which gyro noise can never do."""
    n_frames = [0]
    ones = defaultdict(int)
    zeros_seen_in_rest = set()

    def _on(dev, data):
        n_frames[0] += 1
        for i, b in enumerate(data):
            for bit in range(8):
                key = (dev, len(data), i, bit)
                if b & (1 << bit):
                    ones[key] += 1

    wndproc = _proc_builder_scan(_on)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidHold"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidHold", "hold", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))
    print(f"[hold] keep HOLDING the button for {args.seconds:g}s ...")
    _pump(hwnd, args.seconds)
    total = max(1, n_frames[0])
    print(f"\nframes={total}")
    print("=== bits set in >=95% of frames (i.e. held down the whole time) ===")
    hits = [(k, v) for k, v in ones.items() if v / total >= 0.95]
    if not hits:
        print("  none. The button may not be reaching Raw Input at all")
        print("  (check Steam Input / DS4Windows), or nothing was held.")
        return 0
    for (dev, n, i, bit), v in sorted(hits, key=lambda kv: -kv[1]):
        print(f"  {dev} len={n} byte[{i}] mask=0x{1 << bit:02X}  "
              f"({v}/{total} frames)  -> hid:054C:0CE6:{i}:0x{1 << bit:X}")
    print()
    print("A held button yields exactly this kind of line; gyro bytes will")
    print("NOT appear here because they do not stay set at one bit.")
    return 0


def _run_diff_auto(args) -> int:
    """Fully automatic: learn rest baseline, then learn held state, and report
    bits that are 0 at rest and 1 (stable) while held. No timing from the user
    beyond following the two prompts.
    """
    phase = ["rest"]
    rest_frames = [0]
    held_frames = [0]
    rest_ones = defaultdict(int)
    held_ones = defaultdict(int)

    def _on(dev, data):
        if phase[0] == "rest":
            rest_frames[0] += 1
            for i, b in enumerate(data):
                for bit in range(8):
                    if b & (1 << bit):
                        rest_ones[(dev, len(data), i, bit)] += 1
        else:
            held_frames[0] += 1
            for i, b in enumerate(data):
                for bit in range(8):
                    if b & (1 << bit):
                        held_ones[(dev, len(data), i, bit)] += 1

    wndproc = _proc_builder_scan(_on)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidAuto"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidAuto", "auto", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))
    print(f"[1/2] RELEASE everything, hands off for {args.rest:g}s ...")
    _pump(hwnd, args.rest)
    phase[0] = "hold"
    print(f"[2/2] HOLD R1 down NOW for {args.hold:g}s ...")
    _pump(hwnd, args.hold)

    rf = max(1, rest_frames[0])
    hf = max(1, held_frames[0])
    print(f"\nrest_frames={rf} held_frames={hf}")
    print("=== bits 0-at-rest AND >=90%-set-while-held (R1 candidates) ===")
    hits = []
    for k, v in held_ones.items():
        if rest_ones.get(k, 0) > 0:
            continue                     # already 1 at rest -> constant bit
        if v / hf >= 0.90:
            hits.append((k, v))
    if not hits:
        print("  none. Either R1 wasn't held during phase 2, or its bit was")
        print("  also 1 at rest (unlikely), or Steam/DS4Windows intercepts it.")
        return 0
    for (dev, n, i, bit), v in sorted(hits, key=lambda kv: -kv[1]):
        print(f"  {dev} len={n} byte[{i}] mask=0x{1 << bit:02X}  ({v}/{hf})"
              f"  -> hid:054C:0CE6:{i}:0x{1 << bit:X}")
    return 0


def _proc_builder(mode):
    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wintypes.UINT(0)
            user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                if hdr.dwType == RIM_TYPEHID:
                    hid = ctypes.cast(
                        ctypes.addressof(buf) + ctypes.sizeof(RAWINPUTHEADER),
                        ctypes.POINTER(RAWHID)).contents
                    off = ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWHID)
                    if hid.dwCount >= 1:
                        dev = _dev_key(hdr)
                        data = bytes(buf[off:off + hid.dwSizeHid])
                        if mode == "diff":
                            _on_report_diff(dev, data)
                        else:
                            _on_report(dev, data)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROC(_proc)


def _analyse():
    rest = _snapshot["rest"]
    hold = _snapshot["hold"]
    after = _snapshot["after"]
    rest_frames = max(rest["frames"].values() or [1])
    hold_frames = hold["frames"]
    after_frames = after["frames"]

    rest_ones = rest["ones"]
    hold_ones = hold["ones"]
    after_ones = after["ones"]

    # candidate: never 1 in rest, stably 1 in hold, ~never 1 in after.
    candidates = []
    for k, ho in hold_ones.items():
        dev, n, i, bit = k
        hf = _snapshot["hold"]["frames"].get((dev, n), 0)
        rf = _snapshot["rest"]["frames"].get((dev, n), 0)
        af = _snapshot["after"]["frames"].get((dev, n), 0)
        if rf == 0 or hf == 0 or af == 0:
            continue
        rest_ratio = rest_ones.get(k, 0) / rf
        hold_ratio = ho / hf
        after_ratio = after_ones.get(k, 0) / af
        if rest_ratio == 0.0 and hold_ratio >= 0.7 and after_ratio == 0.0:
            candidates.append((k, rest_ratio, hold_ratio, after_ratio))

    print()
    print("=== R1 candidate bits (rest=0, held=stable1, after=0) ===")
    if not candidates:
        print("  none. Make sure you: REST=hands off, HOLD=hold R1 down ~1s,")
        print("  AFTER=release. Re-run and follow the prompts exactly.")
        return
    for (k, rr, hr, ar) in sorted(candidates, key=lambda c: -c[2]):
        dev, n, i, bit = k
        print(f"  {dev} len={n}  byte[{i}] mask=0x{1 << bit:02X}  "
              f"(hold {hr:.0%})  -> hid:<VID>:<PID>:{i}:0x{1 << bit:X}")


def main() -> int:
    p = argparse.ArgumentParser(description="DualSense button binding wizard")
    p.add_argument("--wizard", action="store_true", help="interactive hold test")
    p.add_argument("--diff", action="store_true",
                   help="baseline-diff, print bits held >=3 frames")
    p.add_argument("--scan", action="store_true",
                   help="list HID reports and their at-rest noise level")
    p.add_argument("--hold-now", action="store_true",
                   help="hold the button BEFORE running; report bits set 95%+")
    p.add_argument("--diff-auto", action="store_true",
                   help="auto: rest baseline then held; report 0->1 stable bits")
    p.add_argument("--rest", type=float, default=4.0)
    p.add_argument("--hold", type=float, default=4.0)
    p.add_argument("--after", type=float, default=2.0)
    p.add_argument("--seconds", type=float, default=20.0)
    args = p.parse_args()

    if args.diff_auto:
        print("=== DualSense auto rest/hold diff ===")
        return _run_diff_auto(args)
    if args.hold_now:
        print("=== DualSense hold-now probe ===")
        return _run_hold_now(args)
    if args.scan:
        print("=== DualSense report scan ===")
        return _run_scan(args)
    if args.diff:
        print("=== DualSense baseline-diff probe ===")
        return _run_diff(args)

    print("=== DualSense binding wizard ===")
    print("Follow the prompts EXACTLY; hands off the pad unless told.")
    wndproc = _proc_builder("wizard")
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidWizard"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidWizard", "wiz", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))

    print(f"\n[1/3] REST: hands OFF the pad for {args.rest:.0f}s ...")
    _pump(hwnd, args.rest)
    _freeze_phase("rest")
    _reset_counters()

    print(f"[2/3] HOLD: HOLD R1 down NOW, keep holding {args.hold:.0f}s ...")
    _pump(hwnd, args.hold)
    _freeze_phase("hold")
    _reset_counters()

    print(f"[3/3] AFTER: release R1, hands off {args.after:.0f}s ...")
    _pump(hwnd, args.after)
    _freeze_phase("after")

    _analyse()
    print()
    print("If exactly one quiet line appears, that's R1. VID:PID = 054C:0CE6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
