"""Read DS5 (DualSense) gamepad via XInput using only ctypes (no deps).

Polls continuously and prints button changes. Use it to verify:
  1. Windows/XInput sees the controller at all
  2. Whether L1 can be read while a fullscreen game is in focus

Run: py -3.12 probe_gamepad.py
Press Ctrl+C to stop.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


# XInput button bitmasks
BUTTONS = {
    0x0001: "DPAD_UP",
    0x0002: "DPAD_DOWN",
    0x0004: "DPAD_LEFT",
    0x0008: "DPAD_RIGHT",
    0x0010: "START",
    0x0020: "BACK",
    0x0040: "LEFT_THUMB",
    0x0080: "RIGHT_THUMB",
    0x0100: "LEFT_SHOULDER",   # LB / L1
    0x0200: "RIGHT_SHOULDER",  # RB / R1
    0x1000: "A",   # Cross
    0x2000: "B",   # Circle
    0x4000: "X",   # Square
    0x8000: "Y",   # Triangle
}


def load_xinput():
    for name in ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(name), name
        except OSError:
            continue
    return None, None


def main():
    lib, name = load_xinput()
    if lib is None:
        print("XInput DLL not found")
        return 1
    print(f"loaded {name}")

    get_state = lib.XInputGetState
    get_state.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
    get_state.restype = wintypes.DWORD

    print("polling... press L1 (index 0). Ctrl+C to stop.\n")
    last_buttons = None
    last_connected = None
    try:
        while True:
            state = XINPUT_STATE()
            # Try all 4 slots, report any connected controller.
            found = False
            for idx in range(4):
                res = get_state(idx, ctypes.byref(state))
                if res == 0:
                    found = True
                    btns = state.Gamepad.wButtons
                    if last_buttons is None or btns != last_buttons:
                        names = [n for bit, n in BUTTONS.items() if btns & bit]
                        l1 = "L1=按下" if btns & 0x0100 else "L1=松开"
                        print(f"[pad {idx}] packet={state.dwPacketNumber} "
                              f"buttons={names} {l1}")
                        last_buttons = btns
                    break
            if not found and last_connected is not False:
                print("no controller detected on slots 0-3")
                last_connected = False
            elif found and last_connected is False:
                last_connected = True
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
