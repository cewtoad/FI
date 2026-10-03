"""XInputSource tests - hermetic via a fake XInput DLL object.

The real path loads xinput1_4.dll through ctypes (OS built-in); these tests
monkeypatch inputs.xinput._load_xinput with a scripted fake, so no hardware
is needed.
"""

from __future__ import annotations

import ctypes
import itertools
import pytest

import inputs.xinput as xi
from inputs.xinput import (_BUTTON_BITS, XInputSource, _TRIGGER_THRESHOLD,
                           capture_xinput_binding)


class FakeDLL:
    """Deterministic XInputGetState stub.

    states: {user_index: (wButtons, lt, rt) | None} - repeated on every call
    (None = device not connected). flip_after: when set, every slot reports
    `pressed` once call count exceeds it (simulates a mid-capture press).
    """

    def __init__(self, states=None, flip_after=None,
                 idle=(0, 0, 0), pressed=(0x1000, 0, 0)):
        self.states = states if states is not None else {}
        self.flip_after = flip_after
        self.idle = idle
        self.pressed = pressed
        self.calls = 0

    def XInputGetState(self, idx, ptr):
        self.calls += 1
        if self.flip_after is not None and self.calls > self.flip_after:
            s = self.pressed
        else:
            s = self.states.get(idx, self.idle)
        st = ctypes.cast(ptr, ctypes.POINTER(xi.XINPUT_STATE)).contents
        if s is None:
            st.Gamepad.wButtons = 0
            st.Gamepad.bLeftTrigger = 0
            st.Gamepad.bRightTrigger = 0
            return 1167                    # ERROR_DEVICE_NOT_CONNECTED
        st.Gamepad.wButtons = s[0]
        st.Gamepad.bLeftTrigger = s[1]
        st.Gamepad.bRightTrigger = s[2]
        return 0


def _install(monkeypatch, states=None, flip_after=None):
    fake = FakeDLL(states=states, flip_after=flip_after)
    monkeypatch.setattr(xi, "_load_xinput", lambda: fake)
    return fake


def test_available(monkeypatch):
    _install(monkeypatch, [(0, 0, 0)])
    assert XInputSource.available() is True
    monkeypatch.setattr(xi, "_load_xinput", lambda: None)
    assert XInputSource.available() is False


def test_button_press_release_edges(monkeypatch):
    fake = _install(monkeypatch, {0: (0, 0, 0)})
    ev = []
    src = XInputSource({"type": "xi", "button": "a"},
                       on_press=lambda: ev.append("press"),
                       on_release=lambda: ev.append("release"))
    src._poll_once()
    assert ev == []
    fake.states[0] = (0x1000, 0, 0)            # A down
    src._poll_once()
    src._poll_once()
    assert ev == ["press"]                     # held repeats are debounced
    fake.states[0] = (0, 0, 0)                 # released
    src._poll_once()
    assert ev == ["press", "release"]


def test_dpad_and_trigger_bindings(monkeypatch):
    fake = _install(monkeypatch, {0: (0, 0, 0)})

    dpad = XInputSource({"type": "xi", "button": "dup"})
    fake.states[0] = (0x0001, 0, 0)                 # dpad up
    assert dpad._is_pressed() is True
    fake.states[0] = (0, 0, 0)
    assert dpad._is_pressed() is False

    trig = XInputSource({"type": "xi", "button": "rt"})
    fake.states[0] = (0, 0, _TRIGGER_THRESHOLD - 1)  # just under threshold
    assert trig._is_pressed() is False
    fake.states[0] = (0, 0, _TRIGGER_THRESHOLD)      # at threshold
    assert trig._is_pressed() is True


def test_second_slot_counts_any_pad(monkeypatch):
    # Pad answers on user index 1 only (index 0 empty) - still triggers.
    _install(monkeypatch, {0: None, 1: (0x1000, 0, 0)})
    src = XInputSource({"type": "xi", "button": "a"})
    assert src._is_pressed() is True


def test_binding_parse_format_describe():
    from inputs.bindings import describe_binding, format_binding, parse_binding
    assert parse_binding("xi:rb") == {"type": "xi", "button": "rb"}
    assert parse_binding("xi:RB") == {"type": "xi", "button": "rb"}   # case-insensitive
    assert parse_binding("xi:guide") is None                          # not bindable
    assert parse_binding("xi:") is None
    assert format_binding({"type": "xi", "button": "rb"}) == "xi:rb"
    assert describe_binding("xi:rb") == "Xbox 手柄 RB 右肩键"
    assert describe_binding("xi:dup", "en") == "Xbox controller D-pad Up"
    assert describe_binding("xi:lt") == "Xbox 手柄 LT 左扳机"


def test_make_source_dispatch(monkeypatch):
    import input_sources as IS
    _install(monkeypatch, [(0, 0, 0)])
    src = IS.make_source("xi:a")
    assert isinstance(src, XInputSource)
    assert IS.make_source("xi:a", enable_hid=False) is None
    assert IS.make_source("hid:054C:0CE6:9:0x02") is not None   # DS path intact
    assert IS.make_source("xi:nope") is None


def test_capture_xinput_binding(monkeypatch):
    # Baseline snapshot sees an idle pad; a press arriving after it is captured.
    _install(monkeypatch, {0: (0, 0, 0)}, flip_after=4)   # 4 calls = 1 snapshot
    clock = itertools.count(0, 0.1)
    got = capture_xinput_binding(timeout_s=60, clock=lambda: next(clock),
                                 sleep=lambda s: None)
    assert got == {"binding": "xi:a", "button": "a"}


def test_capture_ignores_baseline_held_until_released(monkeypatch):
    # A held at capture start is baselined -> ignored; timeout because the
    # button never releases inside the window.
    _install(monkeypatch, {0: (0x1000, 0, 0)})
    clock = itertools.count(0, 0.5)
    assert capture_xinput_binding(timeout_s=1, clock=lambda: next(clock),
                                  sleep=lambda s: None) is None


def test_capture_times_out(monkeypatch):
    _install(monkeypatch, {0: (0, 0, 0)})
    clock = itertools.count(0, 0.5)            # 0.5s per poll -> expires fast
    assert capture_xinput_binding(timeout_s=1, clock=lambda: next(clock),
                                  sleep=lambda s: None) is None


def test_capture_without_dll(monkeypatch):
    monkeypatch.setattr(xi, "_load_xinput", lambda: None)
    assert capture_xinput_binding(timeout_s=1) is None


def test_schema_regex_xi():
    from config_schema import _BINDING_RE
    assert _BINDING_RE.match("xi:a")
    assert _BINDING_RE.match("xi:dleft")
    assert not _BINDING_RE.match("xi:guide")
    assert not _BINDING_RE.match("xi:")
