"""Engineer integration: local fast-answer, LLM path, profile handling."""

from __future__ import annotations

from engineer import Engineer
from profiles import PROFILES

SNAP = {
    "session": {"session_uid": 1, "session_type": "Race", "track_id": "Melbourne",
                "total_laps": 15, "player_car_index": 0},
    "latest": {
        "lap": {"current_lap_num": 8, "car_position": 3,
                "last_lap_time_ms": 83055, "current_lap_time_ms": 45231,
                "delta_to_car_in_front_ms": 800, "delta_to_race_leader_ms": 5200,
                "pit_status": "NONE", "num_pit_stops": 0},
        "car": {"tyres_surface_temp_c": [101, 102, 99, 100]},
        "status": {"tyre_compound_actual": "C3", "tyres_age_laps": 8},
        "car2": {},
    },
    "delta": {"delta_ms": 49, "best_lap_num": 7},
    "fuel": {"surplus_laps": 0.5, "data_sufficient": True},
    "trends": {"lap_times_ms": [83055], "best_lap_ms": 83055},
    "leaderboard": [],
    "events": [],
}


class FakeClient:
    configured = True
    last_usage = {"total_tokens": 42}

    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.3, max_tokens=512):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return "llm answer"

    def describe(self):
        return {"base_url": "fake", "model": "fake"}


class FakeConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=""):
        return self._v.get(key, default)

    def get_int(self, key, default):
        return int(self._v.get(key, default))

    def get_float(self, key, default):
        return float(self._v.get(key, default))


def test_local_router_short_circuits_llm():
    client = FakeClient()
    eng = Engineer(client=client, config=FakeConfig({}))
    answer = eng.ask("我现在P几", SNAP)
    assert "P3" in answer
    assert eng.last_source == "local"
    assert client.calls == []  # no LLM call


def test_miss_goes_to_llm():
    client = FakeClient()
    eng = Engineer(client=client, config=FakeConfig({}))
    answer = eng.ask("帮我规划一下进站策略", SNAP)
    assert answer == "llm answer"
    assert eng.last_source == "llm"
    assert len(client.calls) == 1


def test_fast_profile_trims_max_tokens():
    client = FakeClient()
    eng = Engineer(client=client, config=FakeConfig({"PROFILE": "fast"}))
    eng.ask("分析进站策略", SNAP)
    assert client.calls[0]["max_tokens"] == PROFILES["fast"].max_tokens
    assert "快速档" in client.calls[0]["messages"][0]["content"]


def test_cancel_clears_between_asks():
    client = FakeClient()
    eng = Engineer(client=client, config=FakeConfig({}))
    eng.cancel()
    assert eng._cancel.is_set()
    eng.ask("我P几", SNAP)
    assert not eng._cancel.is_set()
