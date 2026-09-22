"""Global hotkey via Windows Raw Input (no hooks, no injection).

Listens for a configured key while the game is fullscreen. Raw Input is the
same standard API the game itself uses; we only *subscribe*, never intercept.

Default key: NUMPAD + (VK 0x6B). Change TRIGGER_VK to remap.

Emits a debounced "tap" event: one full press+release counts as one tap, so the
caller can use tap-to-start / tap-to-stop.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEKEYBOARD = 1

TRIGGER_VK = 0x6B  # VK_ADD (小键盘 +)

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
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]

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


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RawKeyTrigger:
    """Runs a message loop in a background thread, firing on_tap() per tap.

    A "tap" = one complete press+release of the trigger key. This gives
    tap-to-toggle semantics (tap once to start, tap again to stop).
    """

    def __init__(self, on_tap: Callable[[], None], vk: int = TRIGGER_VK,
                 on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None) -> None:
        self.on_tap = on_tap
        self.on_press = on_press
        self.on_release = on_release
        self.vk = vk
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._pressed = False
        self._hwnd = None
        self._wndproc = WNDPROC(self._proc)

    # ------------------------------------------------------------- internals

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wintypes.UINT(0)
            user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                             ctypes.byref(size),
                                             ctypes.sizeof(RAWINPUTHEADER))
                if got != 0xFFFFFFFF and size.value >= ctypes.sizeof(RAWINPUTHEADER):
                    hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                    if hdr.dwType == RIM_TYPEKEYBOARD:
                        kb = ctypes.cast(
                            ctypes.addressof(buf) + ctypes.sizeof(RAWINPUTHEADER),
                            ctypes.POINTER(RAWKEYBOARD)).contents
                        if kb.VKey == self.vk:
                            down = (kb.Flags & 0x01) == 0
                            self._on_key(down)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_key(self, down: bool) -> None:
        if down and not self._pressed:
            self._pressed = True
            if self.on_press:
                self.on_press()
        elif not down and self._pressed:
            self._pressed = False
            if self.on_release:
                self.on_release()
            # a full press+release = one tap
            self.on_tap()

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "F1TRKeyTrigger"
        user32.RegisterClassW(ctypes.byref(wc))

        HWND_MESSAGE = wintypes.HWND(-3)
        self._hwnd = user32.CreateWindowExW(
            0, "F1TRKeyTrigger", "f1tr", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None)
        if not self._hwnd:
            raise RuntimeError(f"CreateWindowExW failed: {kernel32.GetLastError()}")

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = 0x06
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            raise RuntimeError(
                f"RegisterRawInputDevices failed: {kernel32.GetLastError()}")

        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ----------------------------------------------------------------- public

    def setup(self) -> None:
        """Create the window and register Raw Input (must run on the loop thread)."""
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "F1TRKeyTrigger"
        user32.RegisterClassW(ctypes.byref(wc))

        HWND_MESSAGE = wintypes.HWND(-3)
        self._hwnd = user32.CreateWindowExW(
            0, "F1TRKeyTrigger", "f1tr", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None)
        if not self._hwnd:
            raise RuntimeError(f"CreateWindowExW failed: {kernel32.GetLastError()}")

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = 0x06
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = self._hwnd
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            raise RuntimeError(
                f"RegisterRawInputDevices failed: {kernel32.GetLastError()}")

    def run_blocking(self) -> None:
        """Run the message loop on the CURRENT thread (recommended: main thread).

        Raw Input delivery to a message-only window is only reliable when the
        loop runs on the thread that owns the window and was started from the
        main thread; use this instead of start() where possible.
        """
        self.setup()
        self._running = True
        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
