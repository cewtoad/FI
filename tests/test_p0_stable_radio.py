"""P0 stable-radio slice: ask lock, retirement name, fuel semantics, wiring."""

from __future__ import annotations

import inspect
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from app import build_app
from config_schema import SCHEMA_BY_KEY
from contracts import RaceModelState
from engineer import Engineer
from radio_director import RadioDirector
from radio_rules import build_default_rules, _retirement_display_name, RuleCtx
from summariser import Summariser


class _StubClient:
    configured = True

    def __init__(self):
        self.calls = 0
        self.last_usage = {"prompt": 1}

    def chat(self, messages, temperature=0.3, max_tokens=256):
        self.calls += 1
        time.sleep(0.05)
        return f"ok-{self.calls}"

    def describe(self):
        return {"configured": True}


def test_engineer_ask_serialises_history():
    """Two concurrent asks must not interleave history entries."""
    eng = Engineer(client=_StubClient())
    # Force LLM path: disable local_first by using a profile that still has it;
    # feed a question the local router will miss.
    snap = {"latest": {}, "session": {}, "fuel": {}, "trends": {},
            "delta": {}, "events": [], "leaderboard": [], "position_context": {}}
    results = []

    def worker(q):
        results.append(eng.ask(q, snap))

    t1 = threading.Thread(target=worker, args=("hello concurrent A xyz",))
    t2 = threading.Thread(target=worker, args=("hello concurrent B xyz",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert len(results) == 2
    # History must be well-formed alternating user/assistant pairs.
    assert len(eng.history) == 4
    roles = [m["role"] for m in eng.history]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_ask_and_ask_voice_share_semaphore_source():
    import webui
    src = inspect.getsource(webui._Handler._ask_voice)
    assert "_ASK_SEMAPHORE.acquire" in src
    assert "busy, one question at a time" in src


class _Cfg:
    def get(self, key, default=""):
        return {"RADIO_MIN_GAP_S": "0", "RADIO_VERBOSITY": "chatty",
                "RADIO_PER_LAP_CAP": "20"}.get(key, default)
    def get_bool(self, key, default=False):
        return default if key != "RADIO_ENABLE" else True
    def get_int(self, key, default=0):
        try:
            return int(self.get(key, default))
        except Exception:
            return default
    def get_float(self, key, default=0.0):
        try:
            return float(self.get(key, default))
        except Exception:
            return default


def test_retirement_uses_driver_name_not_car_idx():
    out = []
    d = RadioDirector(build_default_rules(), alert_sink=out.append,
                      config=_Cfg())
    # Warm-up tick so lap_summary consumes its one-shot for this lap marker.
    base = {
        "race_model": {"session_kind": "race", "flags": {}, "upto_lap": 5},
        "latest": {"lap": {"current_lap_num": 5}, "damage": {}, "car2": {},
                   "status": {}},
        "session": {"session_kind": "race", "session_uid": 1},
        "fuel": {},
        "events": [],
        "leaderboard": [],
    }
    d.tick(base, 0.0)
    out.clear()
    snap = dict(base)
    snap["events"] = [{"seq": 1, "kind": "retirement", "vehicle_idx": 3,
                       "is_player": False, "driver": "维斯塔潘",
                       "text": "维斯塔潘退赛"}]
    d.tick(snap, 1.0)
    texts = [a.text for a in out]
    assert any("维斯塔潘" in t for t in texts), texts
    assert not any("car3" in t for t in texts), texts


def test_retirement_display_name_falls_back_to_leaderboard():
    class Names:
        def name_from_index(self, idx, participants):
            return f"car{idx}"

    ctx = RuleCtx(prev=None, curr=None, snapshot={
        "leaderboard": [{"car_index": 7, "driver": "诺里斯"}]
    }, new_events=[], now=0.0, settings=None, names=Names())
    assert _retirement_display_name(ctx, {"vehicle_idx": 7, "driver": "car7"}) == "诺里斯"


def test_race_model_field_is_race_laps_not_fuel():
    assert hasattr(RaceModelState, "__dataclass_fields__")
    fields = RaceModelState.__dataclass_fields__
    assert "race_laps_remaining" in fields
    assert "fuel_laps_left" not in fields


def test_summariser_keeps_fuel_and_race_laps_distinct():
    s = Summariser()
    snap = {
        "latest": {
            "lap": {"current_lap_num": 10, "position": 3},
            "status": {"fuel_in_tank_kg": 40.0, "fuel_remaining_laps": 22.5,
                       "tyre_compound_actual": "C3", "tyres_age_laps": 4},
            "car": {"speed_kph": 280, "gear": 7},
            "car2": {},
            "session": {},
        },
        "session": {"total_laps": 50, "session_kind": "race"},
        "fuel": {"surplus_laps": 1.5},
        "trends": {},
        "delta": {},
        "events": [],
        "leaderboard": [],
        "position_context": {},
        "race_model": {"race_laps_remaining": 41},
    }
    facts = s.summarise(snap)["facts"]
    # Tank range stays under the flat fuel fact.
    assert facts.get("fuel_laps_left") == 22.5
    # Session distance is laps_remaining, never fuel.laps_to_end.
    assert facts.get("laps_remaining") == 41  # 50 - 10 + 1
    assert "fuel.laps_to_end" not in facts
    assert "fuel.laps_left" not in facts


def test_run_py_routes_through_build_app():
    import run
    src = inspect.getsource(run)
    assert "build_app(" in src
    assert "TelemetryState(" not in src


def test_build_app_console_mode_has_radio():
    application = build_app(port=0, recording=False, mode="console", logger=None)
    assert application.radio is not None
    assert application.race_model is not None
    assert application.ticker is not None
    application.shutdown()


def test_assembly_failures_log_warning():
    src = inspect.getsource(__import__("app")._assemble_pipeline)
    assert 'logger.warning("race model unavailable' in src
    assert 'logger.warning("radio director unavailable' in src
    assert 'logger.warning("speech arbiter unavailable' in src


def test_config_schema_has_drifted_voice_keys():
    for key in ("TTS_PROVIDER", "TTS_SAPI_VOICE", "TTS_TIMEOUT",
                "STT_LANGUAGE", "STT_TIMEOUT"):
        assert key in SCHEMA_BY_KEY, key
    assert SCHEMA_BY_KEY["TTS_PROVIDER"].choices == ("auto", "sapi", "piper", "off")
