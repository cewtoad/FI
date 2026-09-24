"""T6: PTT state machine, input-source bindings, HID parsing, names."""

from __future__ import annotations

import input_sources as IS
from names import NameRenderer
from ptt_controller import PTTController


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def adv(self, dt):
        self.t += dt


# ------------------------------------------------------------ PTT controller

def test_hold_mode_press_release():
    c = Clock()
    p = PTTController(mode="hold", clock=c)
    a = p.on_press()
    assert a.kind == "start_recording" and p.recording
    c.adv(1.0)
    a = p.on_release()
    assert a.kind == "stop_recording" and not p.recording


def test_toggle_mode_short_press_toggles():
    c = Clock()
    p = PTTController(mode="toggle", clock=c)
    p.on_press(); c.adv(0.1)
    a = p.on_release()
    assert a.kind == "start_recording" and p.recording
    c.adv(1.0)  # past the double-tap window
    p.on_press(); c.adv(0.1)
    a = p.on_release()
    assert a.kind == "stop_recording" and not p.recording


def test_toggle_double_tap_is_quiet_not_recording():
    c = Clock()
    p = PTTController(mode="toggle", double_tap_window_ms=400, clock=c)
    p.on_press(); c.adv(0.1); p.on_release()   # first tap
    c.adv(0.2)                                  # within window
    p.on_press(); c.adv(0.1)
    a = p.on_release()
    assert a.kind == "toggle_quiet"
    assert not p.recording


def test_long_press_is_not_a_tap():
    c = Clock()
    p = PTTController(mode="toggle", clock=c)
    p.on_press(); c.adv(0.5)   # > TAP_MAX_MS
    a = p.on_release()
    assert a.kind == "none"


# ------------------------------------------------------------- input sources

def test_parse_and_format_bindings():
    kb = IS.parse_binding("kb:0x6B")
    assert kb == {"type": "kb", "vk": 0x6B}
    assert IS.format_binding(kb) == "kb:0x6B"
    hid = IS.parse_binding("hid:054C:0CE6:8:0x20")
    assert hid == {"type": "hid", "vid": 0x054C, "pid": 0x0CE6, "byte": 8, "mask": 0x20}
    assert IS.format_binding(hid) == "hid:054C:0CE6:8:0x20"
    assert IS.parse_binding("garbage") is None
    assert IS.parse_binding("") is None


def test_hid_button_pressed_from_fake_report():
    report = bytes([0, 0, 0, 0, 0, 0, 0, 0, 0x20, 0])
    assert IS.hid_button_pressed(report, 8, 0x20) is True
    assert IS.hid_button_pressed(report, 8, 0x10) is False
    assert IS.hid_button_pressed(report, 99, 0x01) is False   # out of range


def test_make_source_hid_disabled_until_probe():
    # HID disabled by default (probe gate).
    assert IS.make_source("hid:054C:0CE6:8:0x20") is None
    # Keyboard always available.
    src = IS.make_source("kb:0x6B")
    assert src is not None


# ------------------------------------------------------------------- names

def test_name_renderer_styles():
    r = NameRenderer(style="zh")
    assert r.render("NORRIS") == "诺里斯"
    assert r.render("NORRIS", style="en") == "NORRIS"
    assert r.render("NORRIS", style="number") == "4"
    assert r.render(4) == "诺里斯"
    # Unknown passes through.
    assert r.render("UNKNOWN_DRIVER") == "UNKNOWN_DRIVER"


def test_name_from_index_uses_participants():
    r = NameRenderer(style="zh")
    parts = {3: {"name": "LANDO NORRIS", "race_number": 4}}
    assert r.name_from_index(3, parts) == "诺里斯"
    assert r.name_from_index(9, {}) == "car9"
