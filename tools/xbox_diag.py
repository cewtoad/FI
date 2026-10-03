"""Xbox controller Raw Input diagnostic - carry to any PC, stdlib only.

Read-only (RIDEV_INPUTSINK on a message-only window), never injects input.

Modes:
  --scan N     listen N seconds: print every HID device that delivers reports
               (name/VID/PID), report lengths, and hex of each CHANGED report.
               Answers: does Raw Input see the pad at all? USB or BT? sparse
               (change-only) or continuous stream?
  --press      guided button mapping: for each button it prompts
               rest -> hold -> release and prints the (byte, mask) bits that
               turned on. At the end prints a ready-to-paste hid: binding table.

Run:  py -3.12 tools/xbox_diag.py --scan 10
      py -3.12 tools/xbox_diag.py --press
(the F1 full pack also works:  python.exe tools/xbox_diag.py --press)
"""

from __future__ import annotations

import argparse
import ctypes
import re
import sys
import time
from ctypes import wintypes

WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEHID = 2
RIDI_DEVICENAME = 0x20000007

if sys.platform != "win32":
    print("Windows only.")
    raise SystemExit(1)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                          ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.UINT)]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", wintypes.INT), ("cbWndExtra", wintypes.INT),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWHID(ctypes.Structure):
    _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD)]


def read_device_name(hdevice) -> str:
    """Pre-sized single call - the NULL-buffer two-call pattern does not work
    for RIDI_DEVICENAME (it silently returns nothing)."""
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


_VP = re.compile(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", re.IGNORECASE)


def vid_pid(path: str):
    m = _VP.search(path or "")
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


def make_window(proc):
    wndproc = WNDPROC(proc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = f"F1TRXboxDiag{id(wndproc):X}"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "f1tr xbox diag", 0,
                                  0, 0, 0, 0, wintypes.HWND(-3), None,
                                  hinst, None)
    if not hwnd:
        raise RuntimeError("CreateWindowExW failed")
    arr = (RAWINPUTDEVICE * 2)()
    for i, usage in enumerate((0x05, 0x04)):   # gamepad, joystick/wheel
        arr[i].usUsagePage = 0x01
        arr[i].usUsage = usage
        arr[i].dwFlags = RIDEV_INPUTSINK
        arr[i].hwndTarget = hwnd
    if not user32.RegisterRawInputDevices(arr, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        raise RuntimeError("RegisterRawInputDevices failed")
    return hwnd, wndproc


def pump(hwnd, seconds, on_timeout=None):
    """PeekMessage pump (never GetMessage+PostQuitMessage - a WM_QUIT left in
    this thread's queue would kill the next capture on the same thread)."""
    msg = wintypes.MSG()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if on_timeout:
            on_timeout()
        time.sleep(0.002)


class Listener:
    """Collects reports from ALL gamepad/joystick HID devices."""

    def __init__(self):
        self.paths = {}        # hDevice -> interface path
        self.counts = {}       # (devkey, len, rid) -> n
        self.prev = {}         # devkey -> last data
        self.first = {}        # devkey -> first data
        self.events = []       # (t, devkey, data) every CHANGED report
        self.t0 = time.monotonic()

    def on_report(self, devkey, data):
        self.events.append((time.monotonic() - self.t0, devkey, data))
        self.first.setdefault(devkey, data)
        self.prev[devkey] = data

    def handle(self, hdr, data):
        key = int(hdr.hDevice)
        if key not in self.paths:
            self.paths[key] = read_device_name(hdr.hDevice)
        p = self.paths[key]
        devkey = p or f"hDevice{key}"
        rid = data[0] if data else 0
        self.counts[(devkey, len(data), rid)] = \
            self.counts.get((devkey, len(data), rid), 0) + 1
        if self.prev.get(devkey) != data:
            self.on_report(devkey, data)


def run_listener(proc_state):
    """proc_state: object with handle(hdr, data). Returns after KeyboardInterrupt
    or when stop() is called from the wndproc."""
    state = {"stop": False}

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
                            hid = ctypes.cast(
                                ctypes.addressof(buf)
                                + ctypes.sizeof(RAWINPUTHEADER),
                                ctypes.POINTER(RAWHID)).contents
                            off = (ctypes.sizeof(RAWINPUTHEADER)
                                   + ctypes.sizeof(RAWHID))
                            if hid.dwCount >= 1 and hid.dwSizeHid > 0:
                                proc_state.handle(
                                    hdr, bytes(buf[off:off + hid.dwSizeHid]))
            except Exception as e:  # keep the loop alive
                print(f"  [!] parse error: {e!r}", flush=True)
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hwnd, keep = make_window(_proc)
    proc_state._wndproc_keep = keep   # keep the ctypes callback alive!
    return hwnd, state


# ------------------------------------------------------------- scan mode

def cmd_scan(seconds: float) -> int:
    class S(Listener):
        def on_report(self, devkey, data):
            Listener.on_report(self, devkey, data)
            t = self.events[-1][0]
            print(f"  t={t:7.2f}s {devkey[:80]} len={len(data)} "
                  f"hex={data.hex(' ')}", flush=True)

    lis = S()
    hwnd, _ = run_listener(lis)
    print(f"=== scan {seconds:g}s - 动一动摇杆/按几个键，其余时间静置 ===",
          flush=True)
    pump(hwnd, seconds)
    user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
    print("\n=== 设备（Raw Input 眼中的 HID 接口）===")
    for k, p in lis.paths.items():
        vp = vid_pid(p)
        print(f"  {p or '(name unreadable)'}  VID:PID={vp}")
    print("=== 报文统计（设备 x 长度 x report_id -> 帧数）===")
    for (dev, n, rid), c in sorted(lis.counts.items(), key=lambda kv: -kv[1]):
        span = lis.events[-1][0] if lis.events else 0
        rate = c / span if span else 0
        print(f"  len={n:3d} rid=0x{rid:02X} x{c:6d} (~{rate:5.0f} 帧/秒)  "
              f"{dev[:60]}")
    if not lis.counts:
        print("  (没有任何报文 —— 手柄未通过 Raw Input 送达；检查连接/驱动)")
    return 0


# ------------------------------------------------------------- press mode

BUTTONS = [
    ("A", "A（右下脸键）"), ("B", "B（右下）"), ("X", "X（左下）"), ("Y", "Y（上）"),
    ("LB", "LB 左肩键"), ("RB", "RB 右肩键"),
    ("LT", "LT 左扳机（拉到底）"), ("RT", "RT 右扳机（拉到底）"),
    ("Back", "Back/View（左中）"), ("Start", "Start/Menu（右中）"),
    ("LS", "左摇杆按下"), ("RS", "右摇杆按下"),
    ("DUP", "十字键 上"), ("DDOWN", "十字键 下"),
    ("DLEFT", "十字键 左"), ("DRIGHT", "十字键 右"),
    ("Guide", "Xbox 徽标键（可跳过）"),
]
REST_S, HOLD_S, AFTER_S = 0.7, 1.4, 0.5


def cmd_press() -> int:
    class S(Listener):
        pass

    lis = S()
    hwnd, _ = run_listener(lis)
    mapping = {}   # button -> {devkey: [(byte, mask), ...]}
    frames_seen = 0

    def do_phase(seconds):
        pump(hwnd, seconds)

    print("=== 逐键测绘：按提示操作即可，其余时间手放开 ===")
    for btn, label in BUTTONS:
        skip = input(f"\n[{btn}] {label} —— 回车开始（输 s 跳过）：").strip().lower()
        if skip == "s":
            print(f"  [{btn}] 跳过")
            continue
        lis.prev.clear()
        lis.events.clear()
        print(f"  [1/3] 松开所有键 {REST_S:g}s ...", flush=True)
        do_phase(REST_S)
        base = {}
        for dk, d in lis.prev.items():
            base[dk] = d
        lis.events.clear()   # 只测 hold 窗口内的位变化
        print(f"  [2/3] 按住 {label} {HOLD_S:g}s ...", flush=True)
        do_phase(HOLD_S)
        held = {}
        for (t, dk, d) in lis.events:
            b0 = base.get(dk)
            if b0 is None or len(b0) != len(d):
                continue
            for i in range(len(d)):
                x = d[i] ^ b0[i]
                for bit in range(8):
                    if x & (1 << bit) and (d[i] & (1 << bit)):
                        held.setdefault(dk, set()).add((i, 1 << bit))
        print(f"  [3/3] 松开 {AFTER_S:g}s ...", flush=True)
        lis.events.clear()
        do_phase(AFTER_S)
        after = {dk: d for dk, d in lis.prev.items()}
        cands = []
        for dk, bits in held.items():
            a = after.get(dk)
            for (i, mask) in sorted(bits):
                if a is not None and len(a) > i and (a[i] & mask):
                    continue   # release 后仍为 1：不是这个键的稳定位
                cands.append((dk, i, mask))
        if not cands:
            print(f"  [{btn}] 没有捕获到稳定的位变化 —— 记录下来反馈即可")
        else:
            mapping[btn] = cands
            for (dk, i, mask) in cands:
                vp = vid_pid(dk)
                vp_s = f"{vp[0]:04X}:{vp[1]:04X}" if vp else "????"
                print(f"  [{btn}] -> byte[{i}] mask 0x{mask:02X}  "
                      f"({dk[:60]})  hid:{vp_s}:{i}:0x{mask:X}")
        frames_seen += len(lis.events)

    print("\n=== 汇总 hid: 绑定表（可直接写进 PTT_BINDING）===")
    for btn, cands in mapping.items():
        for (dk, i, mask) in cands:
            vp = vid_pid(dk)
            vp_s = f"{vp[0]:04X}:{vp[1]:04X}" if vp else "????"
            print(f"  {btn:7s} hat:{vp_s}:{i}:{mask:#x}? -> hid:{vp_s}:{i}:0x{mask:X}")
    print("\n=== 设备与报文统计（发回这些 + 上面的逐键输出）===")
    for k, p in lis.paths.items():
        print(f"  {p or '(name unreadable)'}")
    for (dev, n, rid), c in sorted(lis.counts.items(), key=lambda kv: -kv[1]):
        print(f"  len={n:3d} rid=0x{rid:02X} x{c:6d}  {dev[:60]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Xbox Raw Input diagnostic")
    ap.add_argument("--scan", type=float, default=10.0,
                    help="listen N seconds and dump reports")
    ap.add_argument("--press", action="store_true",
                    help="guided per-button mapping")
    args = ap.parse_args()
    if args.press:
        return cmd_press()
    return cmd_scan(args.scan)


if __name__ == "__main__":
    sys.exit(main())
