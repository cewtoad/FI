"""PTT binding strings: parse/format for kb/hid/hat bindings (pure).

Binding formats:
    kb:<vk>                        keyboard virtual-key, e.g. kb:0x6B
    hid:VID:PID:byte:mask          a byte/bit in a HID input report, e.g.
                                   hid:054C:0CE6:9:0x02 (DualSense R1)
    hat:VID:PID:byte:value         a hat/dpad nibble VALUE (0-7), e.g.
                                   hat:054C:0CE6:8:2 (DualSense dpad East).
                                   Hats are value-coded (neutral 0x08 has bits
                                   set, North clears all), so a direction
                                   cannot be expressed as a bit mask.
"""

from __future__ import annotations

from typing import Optional


def parse_binding(binding: str) -> Optional[dict]:
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
        if parts[0] == "hat" and len(parts) == 5:
            value = int(parts[4], 0)
            if not 0 <= value <= 7:   # 8 = neutral would mean "always pressed"
                return None
            return {"type": "hat", "vid": int(parts[1], 16), "pid": int(parts[2], 16),
                    "byte": int(parts[3], 0), "value": value}
    except (ValueError, IndexError):
        return None
    return None


def format_binding(desc) -> str:
    if not desc:
        return ""
    if desc["type"] == "kb":
        return f"kb:0x{desc['vk']:02X}"
    if desc["type"] == "hat":
        return (f"hat:{desc['vid']:04X}:{desc['pid']:04X}:"
                f"{desc['byte']}:{desc['value']}")
    return (f"hid:{desc['vid']:04X}:{desc['pid']:04X}:"
            f"{desc['byte']}:0x{desc['mask']:X}")


# ----------------------------------------------------------- human-readable
# Human-readable names so the UI can show "= 手柄 R1" next to the raw binding.

_VK_SPECIAL = {
    0x08: ("退格", "Backspace"), 0x09: ("Tab", "Tab"), 0x0D: ("回车", "Enter"),
    0x13: ("Pause", "Pause"), 0x14: ("大写锁定", "Caps Lock"),
    0x1B: ("Esc", "Esc"), 0x20: ("空格", "Space"),
    0x21: ("翻页上", "Page Up"), 0x22: ("翻页下", "Page Down"),
    0x23: ("End", "End"), 0x24: ("Home", "Home"),
    0x25: ("左方向", "Left"), 0x26: ("上方向", "Up"),
    0x27: ("右方向", "Right"), 0x28: ("下方向", "Down"),
    0x2D: ("Insert", "Insert"), 0x2E: ("Delete", "Delete"),
    0x5B: ("左 Win", "Left Win"), 0x5C: ("右 Win", "Right Win"),
    0x60: ("小键盘 0", "Numpad 0"), 0x61: ("小键盘 1", "Numpad 1"),
    0x62: ("小键盘 2", "Numpad 2"), 0x63: ("小键盘 3", "Numpad 3"),
    0x64: ("小键盘 4", "Numpad 4"), 0x65: ("小键盘 5", "Numpad 5"),
    0x66: ("小键盘 6", "Numpad 6"), 0x67: ("小键盘 7", "Numpad 7"),
    0x68: ("小键盘 8", "Numpad 8"), 0x69: ("小键盘 9", "Numpad 9"),
    0x6A: ("小键盘 *", "Numpad *"), 0x6B: ("小键盘 +", "Numpad +"),
    0x6D: ("小键盘 -", "Numpad -"), 0x6E: ("小键盘 .", "Numpad ."),
    0x6F: ("小键盘 /", "Numpad /"),
    0x90: ("数字锁定", "Num Lock"), 0x91: ("滚动锁定", "Scroll Lock"),
    0xA0: ("左 Shift", "Left Shift"), 0xA1: ("右 Shift", "Right Shift"),
    0xA2: ("左 Ctrl", "Left Ctrl"), 0xA3: ("右 Ctrl", "Right Ctrl"),
    0xA4: ("左 Alt", "Left Alt"), 0xA5: ("右 Alt", "Right Alt"),
}

_DUALSENSE = (0x054C, 0x0CE6)
_DUALSENSE_BITS = {  # (byte, mask) -> (zh, en); DualSense USB layout
    (8, 0x10): ("□ 方块", "Square"), (8, 0x20): ("✕ 叉", "Cross"),
    (8, 0x40): ("○ 圈", "Circle"), (8, 0x80): ("△ 三角", "Triangle"),
    (9, 0x01): ("L1", "L1"), (9, 0x02): ("R1", "R1"),
    (9, 0x04): ("L2", "L2"), (9, 0x08): ("R2", "R2"),
    (9, 0x10): ("Create/分享", "Create/Share"),
    (9, 0x20): ("Options/菜单", "Options"),
    (9, 0x40): ("L3（左摇杆按下）", "L3 (stick click)"),
    (9, 0x80): ("R3（右摇杆按下）", "R3 (stick click)"),
    (10, 0x01): ("PS 键", "PS"), (10, 0x02): ("触摸板按下", "Touchpad click"),
    (10, 0x04): ("静音键", "Mute"),
}
_DUALSENSE_HAT = {0: ("上", "Up"), 1: ("右上", "Up-Right"), 2: ("右", "Right"),
                  3: ("右下", "Down-Right"), 4: ("下", "Down"),
                  5: ("左下", "Down-Left"), 6: ("左", "Left"),
                  7: ("左上", "Up-Left")}


def _vk_name(vk: int, lang: str) -> str:
    zh = lang != "en"
    if 0x41 <= vk <= 0x5A:
        return f"{chr(vk)} 键" if zh else f"Key {chr(vk)}"
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x7B:
        return f"F{vk - 0x6F}"
    name = _VK_SPECIAL.get(vk)
    if name:
        return name[0] if zh else name[1]
    return f"VK 0x{vk:02X}"


def describe_binding(binding: str, lang: str = "zh") -> str:
    """Human-readable name for a binding string (UI display); falls back to
    the raw string for unknown/invalid bindings."""
    desc = parse_binding(binding)
    if not desc:
        return binding or ""
    zh = lang != "en"
    if desc["type"] == "kb":
        return f"键盘 {_vk_name(desc['vk'], lang)}" if zh \
            else f"Keyboard {_vk_name(desc['vk'], lang)}"
    if desc.get("vid") == 0x045E:
        pad = "Xbox 手柄" if zh else "Xbox controller"
    else:
        pad = "手柄" if zh else "Gamepad"
    if (desc.get("vid"), desc.get("pid")) == _DUALSENSE:
        if desc["type"] == "hat":
            d = _DUALSENSE_HAT.get(desc.get("value"))
            if d:
                return f"{pad} 十字键 {d[0]}" if zh else f"{pad} D-pad {d[1]}"
        else:
            nm = _DUALSENSE_BITS.get((desc.get("byte"), desc.get("mask")))
            if nm:
                return f"{pad} {nm[0] if zh else nm[1]}"
        if zh:
            return f"{pad} byte{desc.get('byte')} 位 0x{desc.get('mask', 0):X}"
        return f"{pad} byte{desc.get('byte')} bit 0x{desc.get('mask', 0):X}"
    # Unknown device: keep it technical.
    if desc["type"] == "hat":
        if zh:
            return f"{pad} hat byte{desc['byte']} 值 {desc['value']}"
        return f"{pad} hat byte{desc['byte']} value {desc['value']}"
    if zh:
        return f"{pad} byte{desc['byte']} 位 0x{desc['mask']:X}"
    return f"{pad} byte{desc['byte']} bit 0x{desc['mask']:X}"
