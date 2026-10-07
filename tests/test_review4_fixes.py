"""Fourth review round fixes: dead config keys made real + small robustness.

  - RADIO_PER_LAP_CAP / RADIO_GATE_ENABLE / RADIO_GATE_MAX_WAIT_S consumed
  - RADIO_GAP_MODE=on_change implemented
  - PIT_WINDOW_WARN_LAPS -> new pit_window_warn rule
  - RADIO_BEEP/RADIO_FILTER wired into the voice speech arbiter
  - TTS_RATE / TTS_VOLUME consumed
  - practice_long_run once per stint; unserved_penalty transition-scoped
  - fuel deficit threshold shared with the local router
  - XInput illegal binding no longer raises
"""

from __future__ import annotations

import time

import pytest

import speech
from contracts import PRIORITY_P2, Utterance
from profiles import LocalRouter
from radio_director import RadioDirector
from radio_rules import build_default_rules


class _Cfg:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=""):
        return self._values.get(key, default)

    def get_bool(self, key, default=False):
        v = self._values.get(key)
        if v is None:
            return default
        return str(v).lower() in ("1", "true", "yes", "on")

    def get_int(self, key, default):
        try:
            return int(self._values.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key, default):
        try:
            return float(self._values.get(key, default))
        except (TypeError, ValueError):
            return default


def _snap(model=None, lap=None, events=None, session=None, fuel=None):
    return {
        "race_model": model or {},
        "latest": {"lap": lap or {}, "damage": {}, "car2": {}, "status": {}},
        "session": session or {},
        "fuel": fuel or {},
        "events": events or [],
        "leaderboard": [],
    }


def _collect(cfg=None, rules=None):
    out = []
    d = RadioDirector(rules or build_default_rules(), alert_sink=out.append,
                      config=cfg or _Cfg({}))
    return d, out


# ------------------------------------------------------- per-lap cap config

def test_per_lap_cap_config_can_tighten():
    cfg = _Cfg({"RADIO_VERBOSITY": "chatty", "RADIO_MIN_GAP_S": "0",
                "RADIO_PER_LAP_CAP": "1"})
    d, out = _collect(cfg=cfg)
    # Warm-up tick establishes the lap marker (may emit one lap-summary).
    d.tick(_snap({"session_kind": "race", "flags": {}, "upto_lap": 2},
                 lap={"current_lap_num": 3}), 0.0)
    n0 = len(out)
    for i in range(4):
        ev = {"seq": i + 1, "kind": "position_up", "text": f"up {i}"}
        d.tick(_snap({"session_kind": "race", "flags": {}, "upto_lap": 2},
                     lap={"current_lap_num": 3}, events=[ev]), float(10 + i))
    assert d._per_lap_cap() == 1
    assert len(out) - n0 <= 1, [a.id for a in out]


def test_per_lap_cap_zero_silences_non_p0():
    cfg = _Cfg({"RADIO_VERBOSITY": "chatty", "RADIO_MIN_GAP_S": "0",
                "RADIO_PER_LAP_CAP": "0"})
    d, out = _collect(cfg=cfg)
    ev = {"seq": 1, "kind": "position_up", "text": "up"}
    d.tick(_snap({"session_kind": "race", "flags": {}, "upto_lap": 2},
                 lap={"current_lap_num": 3}, events=[ev]), 0.0)
    assert out == []


# ---------------------------------------------------------------- gap modes

def test_gap_mode_on_change_fires_only_on_movement():
    d, out = _collect(cfg=_Cfg({"RADIO_GAP_MODE": "on_change",
                                "RADIO_MIN_GAP_S": "0"}))
    base = {"session_kind": "race", "flags": {}, "upto_lap": 3,
            "behind": {"gap_ms_now": 4000}}
    d.tick(_snap({**base, "ahead": {"gap_ms_now": 2000}},
                 lap={"current_lap_num": 3}), 0.0)
    assert not [a for a in out if a.id == "gap_report"]     # no baseline yet
    d.tick(_snap({**base, "ahead": {"gap_ms_now": 2600}},
                 lap={"current_lap_num": 3}), 10.0)
    fires = [a for a in out if a.id == "gap_report"]
    assert len(fires) == 1, [a.id for a in out]             # +600ms -> fire
    d.tick(_snap({**base, "ahead": {"gap_ms_now": 2650}},
                 lap={"current_lap_num": 3}), 20.0)
    assert len([a for a in out if a.id == "gap_report"]) == 1   # +50ms quiet


# ------------------------------------------------------------ pit warn rule

def test_pit_window_warn_rule():
    d, out = _collect()
    model = {"session_kind": "race", "flags": {},
             "pit_window": {"state": "not_open", "ideal_lap": 10,
                            "latest_lap": 12, "rejoin_position": 4}}
    d.tick(_snap(model, lap={"current_lap_num": 7}), 0.0)
    assert not [a for a in out if a.id == "pit_window_warn"]  # 3 to go != 2
    d.tick(_snap(model, lap={"current_lap_num": 8}), 10.0)
    assert [a for a in out if a.id == "pit_window_warn"]      # 2 to go fires


def test_pit_window_warn_respects_config_zero():
    d, out = _collect(cfg=_Cfg({"PIT_WINDOW_WARN_LAPS": "0"}))
    model = {"session_kind": "race", "flags": {},
             "pit_window": {"state": "not_open", "ideal_lap": 10,
                            "latest_lap": 12, "rejoin_position": 4}}
    d.tick(_snap(model, lap={"current_lap_num": 8}), 0.0)
    assert not [a for a in out if a.id == "pit_window_warn"]


# --------------------------------------------------- practice / penalties

def _stint(laps, start=1):
    return {"index": 0, "start_lap": start, "compound": "C3", "laps": laps,
            "wear_rate_pct_per_lap": 1.0, "wear_now_pct": 5.0,
            "pace_degradation_s_per_lap": 0.05, "projected_life_laps": 20.0}


def test_practice_long_run_fires_once_per_stint():
    d, out = _collect()
    for i, laps in enumerate((5, 6, 7, 8)):
        d.tick(_snap({"session_kind": "practice", "flags": {},
                      "upto_lap": laps, "stint": _stint(laps)},
                     lap={"current_lap_num": laps}), float(i * 10))
    fires = [a for a in out if a.id == "practice_long_run"]
    assert len(fires) == 1, [a.id for a in out]


def test_unserved_penalty_fires_on_new_penalty_not_persistent():
    d, out = _collect()
    def lap(dt=0, sg=0, n=5):
        return {"current_lap_num": n, "num_unserved_dt_pens": dt,
                "num_unserved_sg_pens": sg}
    d.tick(_snap({"session_kind": "race", "flags": {}}, lap=lap(0, 0)), 0.0)
    d.tick(_snap({"session_kind": "race", "flags": {}}, lap=lap(1, 0)), 30.0)
    d.tick(_snap({"session_kind": "race", "flags": {}}, lap=lap(1, 0)), 60.0)
    assert len([a for a in out if a.id == "unserved_penalty"]) == 1
    # served (0) then re-issued later: must fire again
    d.tick(_snap({"session_kind": "race", "flags": {}}, lap=lap(0, 0, 8)), 90.0)
    d.tick(_snap({"session_kind": "race", "flags": {}}, lap=lap(1, 0, 9)), 120.0)
    assert len([a for a in out if a.id == "unserved_penalty"]) == 2


# ------------------------------------------------------------- speech bits

class _NullPlayer:
    def stop(self):
        pass

    def is_playing(self):
        return False

    def play(self, *a, **k):
        pass


def test_gate_max_wait_bounds_the_wait():
    arb = speech.SpeechArbiter(None, _NullPlayer(), gate=lambda: False,
                               gate_max_wait=lambda: 0.5)
    utt = Utterance(text="x", priority=PRIORITY_P2, source="test",
                    created_at=time.monotonic(), gated=True)
    t0 = time.monotonic()
    assert arb._wait_for_gate(utt) is False
    elapsed = time.monotonic() - t0
    assert 0.4 < elapsed < 2.0      # bounded by the gate wait, not the 15s expiry


def test_gate_max_wait_open_gate_passes_immediately():
    arb = speech.SpeechArbiter(None, _NullPlayer(), gate=lambda: True,
                               gate_max_wait=lambda: 0.5)
    utt = Utterance(text="x", priority=PRIORITY_P2, source="test",
                    created_at=0.0, gated=True)
    assert arb._wait_for_gate(utt) is True


def test_volume_helpers(monkeypatch):
    np = pytest.importorskip("numpy")
    out = speech._apply_volume(np.array([0.5, -0.5], dtype="float32"), 2.0)
    assert float(out[0]) == pytest.approx(1.0)   # clipped
    monkeypatch.setenv("TTS_VOLUME", "0.5")
    assert speech._volume_from_config() == pytest.approx(0.5)
    monkeypatch.setenv("TTS_VOLUME", "9")
    assert speech._volume_from_config() == pytest.approx(2.0)  # clamped


def test_stuck_synth_head_is_dropped_and_worker_replaced():
    clock_v = [100.0]
    arb = speech.SpeechArbiter(None, _NullPlayer(), clock=lambda: clock_v[0])
    # No start(): no synth workers, so the item stays un-synthesised.
    arb.submit(Utterance(text="hang", priority=PRIORITY_P2, source="test",
                         created_at=clock_v[0]))
    with arb._cv:
        item = arb._heap[0]
        item[4] = clock_v[0] - (arb.SYNTH_TIMEOUT_S + 5)   # claimed long ago
    clock_v[0] += 1.0
    with arb._cv:
        assert arb._drop_dead_head_locked() is True
    assert arb.dropped_failed == 1
    assert not arb._heap
    assert len(arb._synth_workers) == 1        # replacement worker spawned
    arb.stop()


# ------------------------------------------------------------ tts / fuel

def test_tts_rate_from_config(monkeypatch):
    import tts_client
    monkeypatch.setenv("TTS_RATE", "4")
    sapi = tts_client.SapiTTS(voice="Fake Voice")
    assert sapi.rate == 4
    piper = tts_client.PiperTTS(voice="missing.onnx")
    assert piper.length_scale == pytest.approx(1.0 - 4 / 20.0)


def test_fuel_threshold_shares_one_source():
    from summariser import Summariser
    facts = Summariser().summarise(
        {"latest": {}, "session": {}, "fuel": {"surplus_laps": -0.3}})
    assert "fuel_deficit_threshold_laps" in facts["facts"]
    router = LocalRouter()
    # threshold -0.5 -> -0.3 is NOT a deficit
    out = router.answer("油量够吗", {"fuel_surplus_laps": -0.3,
                                    "fuel_deficit_threshold_laps": -0.5})
    assert out is not None and "不足" not in out.text, out
    # default threshold -> deficit
    out2 = router.answer("油量够吗", {"fuel_surplus_laps": -0.3})
    assert out2 is not None and "不足" in out2.text, out2


# --------------------------------------------------------------- xinput guard

def test_xinput_invalid_binding_does_not_raise(monkeypatch):
    import inputs.xinput as xi

    class _Dll:
        def XInputGetState(self, idx, ptr):
            return 0   # pretend a pad is connected with all-zero state

    monkeypatch.setattr(xi, "_load_xinput", lambda: _Dll())
    src = xi.XInputSource({"type": "xi", "button": "not-a-button"})
    assert src._is_pressed() is False


# ------------------------------------------------------------ app wiring

def test_voice_app_wires_gate_wait_and_fx(monkeypatch):
    from app import build_app
    monkeypatch.setenv("RADIO_GATE_ENABLE", "0")
    app = build_app(port=0, recording=False, mode="voice")
    try:
        assert app.speech is not None
        assert app.speech._gate is not None
        assert app.speech._gate() is True          # disabled -> always open
        assert app.speech._gate_max_wait is not None
        assert app.speech._fx is not None          # RADIO_BEEP/FILTER wired
    finally:
        app.shutdown()
