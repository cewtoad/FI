"""DualSense HID report probe (T6.1 / STOP POINT #5).

Registers for Raw Input HID on the gamepad usage page and prints, per button
press, which byte/bit in the input report changed. Run it, press the button you
want to use for PTT, and it prints a binding like ``hid:054C:0CE6:8:0x20``.

This is a diagnostic tool (excluded from the packs). It never injects input.

Usage:
    py -3.12 -m tools.probe_dualsense
    py -3.12 -m tools.probe_dualsense --seconds 30
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
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


_last_report = {}


def _report_proc_builder():
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
                    data_off = ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWHID)
                    if hid.dwCount >= 1:
                        n = hid.dwSizeHid
                        data = bytes(buf[data_off:data_off + n])
                        key = hdr.hDevice
                        prev = _last_report.get(key)
                        if prev is not None and len(prev) == len(data):
                            for i, (a, b) in enumerate(zip(prev, data)):
                                if a != b:
                                    print(f"  byte[{i}]: 0x{a:02X} -> 0x{b:02X} "
                                          f"(changed bits 0x{a ^ b:X})")
                        _last_report[key] = data
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROC(_proc)


def main() -> int:
    p = argparse.ArgumentParser(description="Probe DualSense HID report offsets")
    p.add_argument("--seconds", type=float, default=60.0)
    args = p.parse_args()

    print("Probing HID input (press buttons; press Ctrl+C to stop)")
    print("Each changed byte/bit is printed. Note the byte index + mask.")
    wndproc = _report_proc_builder()
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "F1TRHidProbe"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "F1TRHidProbe", "probe", 0, 0, 0, 0, 0,
                                  wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        print("CreateWindowExW failed")
        return 1

    # Register both gamepad (0x01/0x05) and joystick (0x01/0x04) usage.
    for usage in (0x05, 0x04):
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = usage
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                            ctypes.sizeof(RAWINPUTDEVICE))
        print(f"  register usage 0x{usage:02X}: {'ok' if ok else 'FAILED'}")

    end = time.monotonic() + args.seconds
    msg = wintypes.MSG()
    while time.monotonic() < end:
        # Peek so we can honour the timeout.
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
