"""HidSource unit tests (offline; synthetic DualSense USB reports).

The real device was measured 2026-10-02 (DualSense USB HID layout in inputs/hid.py):
input report 0x01, 64 bytes, R1 = byte 9 mask 0x02, Cross = byte 8 mask 0x20.
These tests lock the edge-detection and dispatch logic; no device required.
"""

import input_sources as IS


def _ds_report(byte8: int = 0x08, byte9: int = 0x00, byte10: int = 0x00,
               byte7: int = 0x00, byte13: int = 0x00) -> bytes:
    """Synthetic DualSense USB input report 0x01 (64 bytes, sticks centred)."""
    r = bytearray(64)
    r[0] = 0x01
    r[1] = r[2] = r[3] = r[4] = 0x80
    r[7] = byte7
    r[8] = byte8
    r[9] = byte9
    r[10] = byte10
    r[13] = byte13
    return bytes(r)


def _source(byte: int = 9, mask: int = 0x02):
    events = []
    src = IS.HidSource({"vid": 0x54C, "pid": 0xCE6, "byte": byte, "mask": mask},
                       on_press=lambda: events.append("press"),
                       on_release=lambda: events.append("release"))
    return src, events


def test_r1_binding_edge_detection():
    src, ev = _source()
    src._feed_report(_ds_report())             # idle frame (250Hz stream)
    assert ev == []
    src._feed_report(_ds_report(byte9=0x02))   # R1 down
    src._feed_report(_ds_report(byte9=0x02))   # still held -> no duplicate
    src._feed_report(_ds_report(byte9=0x02))
    assert ev == ["press"]
    src._feed_report(_ds_report())             # release
    src._feed_report(_ds_report())             # idle again -> no duplicate
    assert ev == ["press", "release"]


def test_short_reports_never_fake_release():
    src, ev = _source()
    src._feed_report(_ds_report(byte9=0x02))
    assert ev == ["press"]
    # A shorter report of the same device (other report ID) must be ignored,
    # not interpreted as "button released".
    src._feed_report(b"\x01\x80")
    assert ev == ["press"]


def test_cross_button_binding():
    src, ev = _source(byte=8, mask=0x20)
    src._feed_report(_ds_report(byte8=0x28))   # hat neutral + Cross
    assert ev == ["press"]
    src._feed_report(_ds_report(byte8=0x06))   # dpad west, Cross released
    assert ev == ["press", "release"]


def test_device_name_matching():
    f = IS.HidSource._name_matches
    assert f("\\\\??\\HID#VID_054C&PID_0CE6&MI_03#8&4eaa62f&0&0000",
             0x54C, 0xCE6)
    assert not f("\\\\??\\HID#VID_054C&PID_0DF2&MI_03#...", 0x54C, 0xCE6)
    assert not f("", 0x54C, 0xCE6)
    assert not f(None, 0x54C, 0xCE6)


def test_make_source_dispatch_and_gate():
    src = IS.make_source("hid:054C:0CE6:9:0x02")
    assert isinstance(src, IS.HidSource)
    # Conservative callers can still gate HID off explicitly.
    assert IS.make_source("hid:054C:0CE6:9:0x02", enable_hid=False) is None
    assert IS.make_source("kb:0x6B") is not None
    assert IS.make_source("hid:not-a-binding") is None
    assert IS.make_source("") is None


# ------------------------------------------------------- capture (wizard)

def test_vid_pid_from_path():
    f = IS.vid_pid_from_path
    assert f("\\\\?\\HID#VID_054C&PID_0CE6&MI_03#8&4eaa62f&0&0000") == (0x54C, 0xCE6)
    assert f("\\\\??\\hid#vid_054c&pid_0ce6&mi_03#...") == (0x54C, 0xCE6)
    assert f("") is None
    assert f("\\\\?\\HID#VID_X&PID_Y") is None


def test_capture_scan_learns_floor_then_captures():
    scan = IS.CaptureScan()
    t = 0.0
    # Baseline: idle stream with the rolling counter byte ticking. Counter
    # bits must land in the floor so they can never be captured.
    for i in range(30):
        assert scan.feed("dev", _ds_report(byte7=(i * 7) & 0xFF), t) is None
        t += 0.03
    # Hold R1 -> candidate becomes pending (not accepted while held)...
    for _ in range(12):
        assert scan.feed("dev", _ds_report(byte9=0x02), t) is None
        t += 0.03
    # ...releasing confirms the binding (bind-on-release).
    want = {"dev": "dev", "kind": "bit", "byte": 9, "mask": 0x02}
    assert scan.feed("dev", _ds_report(), t) == want
    assert scan.feed("dev", _ds_report(), t) == want  # idempotent


def test_capture_scan_rejects_flicker():
    scan = IS.CaptureScan()
    t = 0.0
    for _ in range(30):                       # idle baseline
        assert scan.feed("dev", _ds_report(), t) is None
        t += 0.03
    # A short flicker (held 0.09s < hold_s 0.25s) must not capture...
    for _ in range(3):
        assert scan.feed("dev", _ds_report(byte9=0x10), t) is None
        t += 0.03
    scan.feed("dev", _ds_report(), t)         # released again
    t += 0.03
    # ...and a real sustained press still captures afterwards.
    for _ in range(12):
        scan.feed("dev", _ds_report(byte9=0x02), t)
        t += 0.03
    assert scan.feed("dev", _ds_report(), t) == \
        {"dev": "dev", "kind": "bit", "byte": 9, "mask": 0x02}


def test_capture_scan_button_held_at_baseline_is_uncapturable():
    # Wizard contract: a button already held when capture starts is part of
    # the learned at-rest floor (release everything first).
    scan = IS.CaptureScan()
    t = 0.0
    for _ in range(30):                       # baseline, R1 already held
        scan.feed("dev", _ds_report(byte9=0x02), t)
        t += 0.03
    for _ in range(30):                       # still held after baseline
        assert scan.feed("dev", _ds_report(byte9=0x02), t) is None
        t += 0.03


def test_capture_scan_per_device_and_short_reports():
    scan = IS.CaptureScan()
    t = 0.0
    for _ in range(30):
        scan.feed("padA", _ds_report(), t)
        scan.feed("padB", _ds_report(), t)
        t += 0.03
    assert scan.feed("padA", b"\x01\x80\x80", t) is None    # too short: ignored
    for _ in range(12):                     # Cross on pad B: pending while held
        scan.feed("padB", _ds_report(byte8=0x28), t)
        t += 0.03
    assert scan.feed("padB", _ds_report(), t) == \
        {"dev": "padB", "kind": "bit", "byte": 8, "mask": 0x20}


def test_capture_scan_dualsense_known_byte_range():
    # Regression (2026-10-03): with hands off, the real pad false-captured
    # hid:054C:0CE6:13:0x1 — a payload-counter bit in byte 13. Known-DS scan
    # range must be the documented button bytes 8..10 only.
    scan = IS.CaptureScan()
    path = "\\\\?\\HID#VID_054C&PID_0CE6&MI_03#8&4eaa62f&0&0000"
    t = 0.0
    for _ in range(30):                       # baseline: byte13 bit0 at rest 0
        scan.feed(path, _ds_report(), t)
        t += 0.03
    for _ in range(35):                       # counter bit 1 for ~1s...
        scan.feed(path, _ds_report(byte13=0x01), t)
        t += 0.03
    scan.feed(path, _ds_report(), t)          # ...then back to 0: would pass
    t += 0.03                                 # release-confirm on a generic range
    assert scan.result is None                # -> must be ignored for a DS
    # R1 on the documented button bytes still captures normally.
    for _ in range(12):
        scan.feed(path, _ds_report(byte9=0x02), t)
        t += 0.03
    assert scan.feed(path, _ds_report(), t) == \
        {"dev": path, "kind": "bit", "byte": 9, "mask": 0x02}


def test_capture_scan_dualsense_hat_directions():
    # Dpad is the byte-8 LOW NIBBLE value (neutral 0x08, North 0x00, East
    # 0x02, ...) — value-coded, so bit-scan cannot see directions at all
    # (North CLEARS bits; West/diagonals collapse onto colliding bits).
    path = "\\\\?\\HID#VID_054C&PID_0CE6&MI_03#8&4eaa62f&0&0000"

    def _capture(direction: int) -> dict:
        scan = IS.CaptureScan()
        t = 0.0
        for _ in range(30):                   # baseline: neutral 0x08 floored
            scan.feed(path, _ds_report(), t)
            t += 0.03
        for _ in range(12):                   # deflected: pending while held
            scan.feed(path, _ds_report(byte8=direction), t)
            t += 0.03
        scan.feed(path, _ds_report(), t)      # back to neutral: confirmed
        return scan.result

    assert _capture(0x02) == {"dev": path, "kind": "hat", "byte": 8, "value": 2}
    assert _capture(0x00) == {"dev": path, "kind": "hat", "byte": 8, "value": 0}
    assert _capture(0x06) == {"dev": path, "kind": "hat", "byte": 8, "value": 6}
    # Face button on byte 8's HIGH nibble still bit-captures (Cross 0x20).
    scan = IS.CaptureScan()
    t = 0.0
    for _ in range(30):
        scan.feed(path, _ds_report(), t)
        t += 0.03
    for _ in range(12):
        scan.feed(path, _ds_report(byte8=0x28), t)
        t += 0.03
    assert scan.feed(path, _ds_report(), t) == \
        {"dev": path, "kind": "bit", "byte": 8, "mask": 0x20}


def test_capture_scan_dualsense_quick_tap_timing():
    # L1/R2 regression (2026-10-03): shoulder/trigger taps are naturally short;
    # the old flat 0.25s hold rejected them. Known-DS hold threshold is 0.12s.
    scan = IS.CaptureScan()
    path = "\\\\?\\HID#VID_054C&PID_0CE6&MI_03#8&4eaa62f&0&0000"
    t = 0.0
    for _ in range(20):                    # baseline 0.6s
        scan.feed(path, _ds_report(), t)
        t += 0.03
    # A 3-frame tap (0.09s < 0.12s) is still too short...
    for _ in range(3):
        scan.feed(path, _ds_report(byte9=0x01), t)   # L1
        t += 0.03
    scan.feed(path, _ds_report(), t)
    t += 0.03
    assert scan.result is None
    # ...a 5-frame press (0.15s) captures L1 after release.
    for _ in range(5):
        scan.feed(path, _ds_report(byte9=0x01), t)
        t += 0.03
    scan.feed(path, _ds_report(), t)
    assert scan.result == {"dev": path, "kind": "bit", "byte": 9, "mask": 0x01}


def test_capture_scan_pending_timeout_blacklists_stuck_bit():
    # Generic devices (Xbox-class sparse reports scan bytes 4-15) can hold a
    # counter/timestamp bit high for seconds; the bind-on-release wait must
    # give up on it instead of locking the capture until timeout.
    scan = IS.CaptureScan()
    t = 0.0
    for _ in range(30):                     # baseline: byte13 bit0 at rest 0
        scan.feed("pad", _ds_report(), t)
        t += 0.03
    for _ in range(12):                     # held 0.36s -> pending
        scan.feed("pad", _ds_report(byte13=0x01), t)
        t += 0.03
    for _ in range(90):                     # still "held" way past 2.5s
        scan.feed("pad", _ds_report(byte13=0x01), t)
        t += 0.03
    assert scan._pending.get("pad") is None   # abandoned, not locked
    # The blacklisted bit is excluded; a real button still captures.
    for _ in range(12):
        scan.feed("pad", _ds_report(byte9=0x02, byte13=0x01), t)
        t += 0.03
    scan.feed("pad", _ds_report(byte13=0x01), t)
    assert scan.result == {"dev": "pad", "kind": "bit", "byte": 9, "mask": 0x02}


def test_capture_scan_xbox_vid_timings():
    # Xbox (VID 045E, any PID): tighter timings via the VID-level config —
    # a 0.15s hold (5 frames) captures, where the generic 0.25s would reject.
    scan = IS.CaptureScan()
    path = "\\\\?\\HID#VID_045E&PID_0B12&MI_00#..."
    t = 0.0
    for _ in range(20):                     # baseline 0.6s
        scan.feed(path, _ds_report(), t)
        t += 0.03
    for _ in range(5):                      # A-button candidate held 0.15s
        scan.feed(path, _ds_report(byte9=0x01), t)
        t += 0.03
    scan.feed(path, _ds_report(), t)        # release confirms
    assert scan.result == {"dev": path, "kind": "bit", "byte": 9, "mask": 0x01}


def test_describe_binding_xbox_prefix():
    f = IS.describe_binding
    assert f("hid:045E:0B12:5:0x10") == "Xbox 手柄 byte5 位 0x10"
    assert f("hid:045E:0B12:5:0x10", "en") == "Xbox controller byte5 bit 0x10"


def test_hat_binding_parse_format_and_source():
    assert IS.parse_binding("hat:054C:0CE6:8:2") == \
        {"type": "hat", "vid": 0x54C, "pid": 0xCE6, "byte": 8, "value": 2}
    assert IS.format_binding({"type": "hat", "vid": 0x54C, "pid": 0xCE6,
                              "byte": 8, "value": 2}) == "hat:054C:0CE6:8:2"
    src = IS.make_source("hat:054C:0CE6:8:2")
    assert isinstance(src, IS.HidSource) and src._hat_value == 2
    # Neutral (0x08) must never be accepted as a hat binding.
    assert IS.make_source("hat:054C:0CE6:8:8") is None


def test_hat_source_feed():
    ev = []
    src = IS.HidSource({"type": "hat", "vid": 0x54C, "pid": 0xCE6,
                        "byte": 8, "value": 0},
                       on_press=lambda: ev.append("press"),
                       on_release=lambda: ev.append("release"))
    src._feed_report(_ds_report(byte8=0x00))   # North pressed
    assert ev == ["press"]
    # A hat binding is direction-SPECIFIC: sliding to East reads as releasing
    # North (correct for PTT — the bound direction is no longer held).
    src._feed_report(_ds_report(byte8=0x02))
    assert ev == ["press", "release"]
    src._feed_report(_ds_report(byte8=0x00))   # back to North -> press again
    assert ev == ["press", "release", "press"]
    src._feed_report(_ds_report())             # neutral -> release
    assert ev == ["press", "release", "press", "release"]


def test_hat_binding_schema_regex():
    from config_schema import _BINDING_RE, validate
    assert _BINDING_RE.match("hat:054C:0CE6:8:2")
    assert _BINDING_RE.match("hat:054C:0CE6:8:0")
    assert not _BINDING_RE.match("hat:054C:0CE6:8:8")   # neutral: nonsense
    assert not _BINDING_RE.match("hat:054C:0CE6:8:12")
    assert validate("PTT_BINDING", "hat:054C:0CE6:8:2") == "hat:054C:0CE6:8:2"


def test_describe_binding():
    f = IS.describe_binding
    assert f("kb:0x6B") == "键盘 小键盘 +"
    assert f("kb:0x6B", "en") == "Keyboard Numpad +"
    assert f("kb:0x41") == "键盘 A 键"
    assert f("kb:0x41", "en") == "Keyboard Key A"
    assert f("hid:054C:0CE6:9:0x02") == "手柄 R1"
    assert f("hid:054C:0CE6:9:0x02", "en") == "Gamepad R1"
    assert f("hid:054C:0CE6:8:0x20", "en") == "Gamepad Cross"
    assert f("hat:054C:0CE6:8:2") == "手柄 十字键 右"
    assert f("hat:054C:0CE6:8:0", "en") == "Gamepad D-pad Up"
    assert f("hid:054C:0CE6:9:0x99").startswith("手柄 byte9")   # unknown bit
    assert f("hid:1234:5678:3:0x01").startswith("手柄 byte3")   # unknown pad
    assert f("garbage") == "garbage"                          # fall back to raw
    assert f("") == ""


def test_webui_binding_name_endpoint():
    import inspect

    import webui
    assert "/api/binding_name" in inspect.getsource(webui._Handler)
    assert "/api/binding_name" in webui.PAGE
    assert "updateBindingName" in webui.PAGE


def test_compat_facade_reexports():
    for name in ("InputSource", "KeyboardSource", "HidSource", "parse_binding",
                 "format_binding", "hid_button_pressed", "make_source",
                 "capture_keyboard_binding", "capture_hid_binding",
                 "vid_pid_from_path"):
        assert hasattr(IS, name), name
    assert IS.HidSource is __import__("inputs").HidSource
