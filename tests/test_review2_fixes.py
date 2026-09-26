"""Regression tests for review round 2 (post-v2 audit fixes).

Covers:
  - speech arbiter worker isolation + priority synthesis pick (S1)
  - snapshot reader amplification / publisher reuse (S2)
  - radio director cross-session reset (S3)
  - pit-window "done" latch + laps_to_1s clamp + laps-remaining convention (S4)
  - config schema hardening (newline injection, NaN/inf/Overflow, PTT_BINDING)
  - PTT release-without-press guard
  - rule dedup-key scoping (fastest lap) + gap-every-n=0 silence
  - debrief retry after a failed write
  - FallbackLLM error chain + webui local-only / Host / Origin guards
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from config_schema import validate
from contracts import PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, Utterance
from race_model import RaceModel
from radio_director import RadioDirector
from radio_rules import RuleCtx, build_default_rules


# ---------------------------------------------------------------- speech (S1)

class _FlakyPlayer:
    """play() raises for the 'bad' payload; is_playing() raises once."""

    def __init__(self):
        self.started = []
        self.playing_calls = 0

    def play(self, audio, mime):
        self.started.append(audio)
        if audio == b"bad":
            raise RuntimeError("boom")

    def stop(self):
        pass

    def is_playing(self):
        self.playing_calls += 1
        if self.playing_calls == 1:
            raise RuntimeError("player state exploded")
        return False


class _TTS:
    available = True
    mime = "audio/wav"

    def synthesize(self, text):
        return text.encode()


def _arbiter_run(texts, wait=1.0):
    from speech import SpeechArbiter
    p = _FlakyPlayer()
    a = SpeechArbiter(_TTS(), p)
    a.start()
    try:
        now = time.monotonic()
        for text, prio in texts:
            a.submit(Utterance(text=text, priority=prio, source="rule",
                               created_at=now, gated=False))
        time.sleep(wait)
    finally:
        a.stop()
    return p, a


def test_arbiter_survives_bad_payload():
    p, a = _arbiter_run([("bad", PRIORITY_P1), ("good", PRIORITY_P2)])
    assert b"good" in p.started          # the queue kept moving
    assert a._worker is None or a._worker.is_alive()


def test_arbiter_survives_player_state_error():
    p, a = _arbiter_run([("one", PRIORITY_P2), ("two", PRIORITY_P2)])
    assert p.started == [b"one", b"two"]
    assert a.dropped_failed >= 1         # the _run-level safety net fired


def test_synth_pick_prefers_highest_priority():
    from speech import SpeechArbiter
    from speech import AudioPlayer  # noqa: F401  (import sanity)
    a = SpeechArbiter(_TTS(), _FlakyPlayer())
    now = time.monotonic()
    for text, prio in (("low", PRIORITY_P2), ("p0", PRIORITY_P0),
                       ("mid", PRIORITY_P1)):
        a.submit(Utterance(text=text, priority=prio, source="rule",
                           created_at=now, gated=False))
    best = a._pick_synth_item()
    assert best is not None and best[2].text == "p0"
    a._do_synthesize(best)
    assert best[3] == b"p0"
    nxt = a._pick_synth_item()
    assert nxt is not None and nxt[2].text == "mid"   # claimed item not re-picked
    a.stop()


# ---------------------------------------------------------------- state (S2)

def test_snapshot_rebuilds_without_publisher():
    from state import TelemetryState
    st = TelemetryState()
    s1 = st.snapshot()
    st.mark_dirty()
    s2 = st.snapshot()
    assert s1 is not s2          # read-your-writes for direct drivers/tests


def test_snapshot_reuses_fresh_publisher_copy():
    from state import TelemetryState
    st = TelemetryState()
    st.refresh_snapshot()        # simulates the receiver thread publishing
    st.mark_dirty()              # packets arrived after the published copy
    a = st.snapshot()
    b = st.snapshot()
    assert a is b                # readers share the frozen copy (no rebuild)


def test_flashback_resets_position_baseline():
    from state import TelemetryState
    st = TelemetryState()
    st._last_position = 5
    st._on_event_flashback(None)
    assert st._last_position is None and st._pending_flashback


# ------------------------------------------------------- radio director (S3)

def _snap(events=None, session=None):
    return {
        "race_model": {},
        "latest": {"lap": {}, "damage": {}, "car2": {}, "status": {}},
        "session": session or {},
        "fuel": {},
        "events": events or [],
        "leaderboard": [],
    }


def test_director_resets_for_new_session():
    out = []
    d = RadioDirector(rules=build_default_rules(), alert_sink=out.append)
    ev9 = [{"kind": "safety_car", "event_type": 1, "seq": 9, "text": "x"}]
    ev1 = [{"kind": "safety_car", "event_type": 2, "seq": 1, "text": "y"}]
    d.tick(_snap(ev9, {"session_uid": 111}), 0.0)
    assert any(a.id == "sc_ending" for a in out)
    # Second session: seq restarts at 1 and the cooldown clock is fresh.
    d.tick(_snap(ev1, {"session_uid": 222}), 10.0)
    assert d._last_event_seq == 1
    assert any(a.id == "sc_ending" for a in out[len(out) - 1:])


# ------------------------------------------------------------ race model (S4)

def _rm_snap(samples, cur, ideal, latest, stops, pit="NONE", total=50):
    return {
        "session": {"session_kind": "race", "total_laps": total,
                    "pit_window_ideal_lap": ideal,
                    "pit_window_latest_lap": latest,
                    "pit_window_rejoin_position": 4,
                    "safety_car_status": "NO SAFETY CAR",
                    "weather_forecast": [], "marshal_yellow_zones": []},
        "latest": {"lap": {"current_lap_num": cur, "lap_distance_m": 0.0,
                           "pit_status": pit, "num_pit_stops": stops},
                   "status": {}},
        "fuel": {"surplus_laps": 1.0},
        "lap_snapshots": samples,
        "leaderboard": [],
    }


def _lap(n):
    return {"lap_num": n, "lap_time_ms": 85000, "valid": True,
            "tyre_compound": "C3", "tyre_age_laps": n, "tyre_wear_max_pct": 2.0 * n,
            "position": 3, "gap_ahead_ms": 2000 - n, "ahead_index": 1,
            "gap_behind_ms": 3000, "behind_index": 4,
            "safety_car": "NO SAFETY CAR", "pit_this_lap": False}


def test_pit_window_done_latches_after_stop():
    m = RaceModel()
    m.update(_rm_snap([_lap(1)], cur=9, ideal=10, latest=12, stops=0), 0.0)
    assert m.latest.pit_window.state == "not_open"
    m.update(_rm_snap([_lap(1)], cur=11, ideal=10, latest=12, stops=0,
                      pit="PITTING"), 1.0)
    assert m.latest.pit_window.state == "done"
    # Out of the pits: pit_status is transient, the stop count is not —
    # the state used to regress to open/missed here (false radio alerts).
    m.update(_rm_snap([_lap(1), _lap(2)], cur=12, ideal=10, latest=12,
                      stops=1), 2.0)
    assert m.latest.pit_window.state == "done"
    m.update(_rm_snap([_lap(1), _lap(2), _lap(3)], cur=13, ideal=10,
                      latest=12, stops=1), 3.0)
    assert m.latest.pit_window.state == "done"
    # The window rotates to the next planned stop: the latch re-arms.
    m.update(_rm_snap([_lap(1), _lap(2), _lap(3)], cur=13, ideal=30,
                      latest=32, stops=1), 4.0)
    assert m.latest.pit_window.state == "not_open"


def test_laps_to_1s_is_clamped():
    samples = [_lap(n) for n in range(1, 20)]   # gap shrinks 1ms/lap: noise
    m = RaceModel()
    m.update(_rm_snap(samples, cur=19, ideal=0, latest=0, stops=0, total=50), 0.0)
    laps = m.latest.ahead.laps_to_1s
    assert laps is not None and laps <= 50      # was ~1000 (unbounded noise)


# ------------------------------------------------------------ config schema

def test_schema_rejects_nan_inf_and_overflow():
    with pytest.raises(ValueError):
        validate("TTS_VOLUME", "nan")
    with pytest.raises(ValueError):
        validate("RADIO_GATE_HOLD_S", "inf")
    with pytest.raises(ValueError):
        validate("RADIO_PER_LAP_CAP", "1e999")   # int(inf) OverflowError path
    with pytest.raises(ValueError):
        validate("LLM_API_KEY", "sk-x\nRADIO_ENABLE=0\nLLM_BASE_URL=http://x")


def test_schema_cleans_control_chars_but_keeps_text():
    assert validate("AUDIO_INPUT", "ab\x00cd") == "abcd"
    assert validate("AUDIO_INPUT", "G733 headset") == "G733 headset"


def test_schema_validates_ptt_binding_and_new_llm_keys():
    with pytest.raises(ValueError):
        validate("PTT_BINDING", "kb")
    with pytest.raises(ValueError):
        validate("PTT_BINDING", "hid:123")
    assert validate("PTT_BINDING", "kb:0x6B") == "kb:0x6B"
    assert validate("PTT_BINDING", "hid:054C:0CE6:0x01:0x08")
    assert validate("LLM_TIMEOUT", 45) == 45.0
    for key in ("LLM_TIMEOUT", "LLM_FALLBACK_API_KEY",
                "LLM_FALLBACK_BASE_URL", "LLM_FALLBACK_MODEL",
                "RADIO_FUEL_DEFICIT_LAPS"):
        assert __import__("config_schema").get_setting(key) is not None, key


# ------------------------------------------------------------------ PTT

def test_ptt_release_without_press_does_not_start():
    from ptt_controller import PTTController
    p = PTTController(mode="toggle", clock=lambda: 0.0)
    assert not p.on_release()        # stray release must not start recording
    p.reset()
    assert not p.on_release()
    # The machine still works normally afterwards.
    assert p.on_press().kind == "none"
    assert p.on_release().kind == "start_recording"


# ------------------------------------------------------------- radio rules

def _ctx(events=None, model=None, settings=None, snap=None):
    return RuleCtx(prev=None, curr=model, snapshot=snap or _snap(events),
                   new_events=events or [], now=0.0, settings=settings,
                   names=None)


def test_fastest_lap_dedup_key_is_seq_scoped():
    build_default_rules()
    import radio_rules as R
    ev = [{"kind": "fastest_lap", "is_player": True, "lap_time_ms": 81000,
           "seq": 3}]
    a1 = R._rule_fastest_lap_you(_ctx(events=ev))
    ev2 = [{"kind": "fastest_lap", "is_player": True, "lap_time_ms": 80000,
            "seq": 9}]
    a2 = R._rule_fastest_lap_you(_ctx(events=ev2))
    assert a1 is not None and a2 is not None
    assert a1.dedup_key != a2.dedup_key


def test_gap_every_n_zero_means_silence():
    build_default_rules()
    import radio_rules as R

    class _Cfg:
        def get(self, key, default=""):
            return "every_n_laps" if key == "RADIO_GAP_MODE" else default

        def get_int(self, key, default):
            return 0 if key == "RADIO_GAP_EVERY_N" else default

    model = type("M", (), {"ahead": None, "behind": None})()
    alert = R._rule_gap_report(_ctx(model=model, settings=_Cfg()))
    assert alert is None


# ------------------------------------------------------------------ debrief

class _Cfg2:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=""):
        return self._v.get(key, default)

    def get_bool(self, key, default=False):
        return bool(self._v.get(key, default))


def test_debrief_retries_after_failed_write(tmp_path):
    from debrief import DebriefWriter
    blocker = tmp_path / "sessions"
    blocker.write_text("")           # a FILE where the output dir should be
    w = DebriefWriter(config=_Cfg2({"DEBRIEF_DIR": str(blocker)}))
    snap = {"lap_snapshots": [{"lap_num": 1, "valid": True,
                               "lap_time_ms": 90000}],
            "events": [{"kind": "chequered", "seq": 1, "text": "end"}],
            "session": {}}
    w(snap, 0.0)                     # write fails -> must NOT be marked done
    assert w.last_path is None
    blocker.unlink()
    w(snap, 1.0)                     # retried on the next tick
    assert w.last_path is not None


# ------------------------------------------------------------- llm fallback

def test_fallback_error_includes_primary_cause():
    from llm_client import FallbackLLM, LLMError

    class _C:
        configured = True

        def __init__(self, err):
            self._err = err
            self.last_usage = None
            self.base_url = "http://endpoint"

        def chat(self, *a, **k):
            raise self._err

    prim = _C(LLMError("HTTP 429: rate limited", retryable=True))
    fb = _C(LLMError("Network error: no route", retryable=True))
    f = FallbackLLM(prim, fb)  # type: ignore[arg-type]
    with pytest.raises(LLMError) as ei:
        f.chat([])
    assert "HTTP 429" in str(ei.value) and "fallback" in str(ei.value)


# ------------------------------------------------------------- webui guards

class _FakeHeaders:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


def test_local_only_uses_real_peer_address():
    import webui
    h = object.__new__(webui._Handler)
    h.client_address = ("192.168.1.9", 5000)
    assert h._local_only() is False
    h.client_address = ("127.0.0.1", 5000)
    assert h._local_only() is True
    h.client_address = ("::1", 5000)
    assert h._local_only() is True


def _serve():
    import webui
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    httpd.ctx = {"state": None, "summariser": None, "receiver": None,
                 "engineer": None, "voice": None, "recorder": None}
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, t


def test_host_header_guard_rejects_rebinding_host():
    httpd, t = _serve()
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state")
        req.add_header("Host", "evil.example.com")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 403
    finally:
        httpd.shutdown()


def test_origin_guard_rejects_cross_site_llm_write():
    httpd, t = _serve()
    try:
        port = httpd.server_address[1]
        body = json.dumps({"base_url": "http://x"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/llm",
                                     data=body, method="POST")
        req.add_header("Origin", "http://evil.example.com")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 403
    finally:
        httpd.shutdown()
