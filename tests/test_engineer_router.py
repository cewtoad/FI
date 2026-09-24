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


def test_no_key_gives_actionable_hint_not_dead_end():
    class Unconfigured:
        configured = False
        last_usage = None

        def chat(self, *a, **k):
            raise AssertionError("must not call LLM without a key")

        def describe(self):
            return {}

    eng = Engineer(client=Unconfigured(), config=FakeConfig({}))
    # A question the local router cannot answer -> actionable hint.
    answer = eng.ask("帮我规划进站策略", SNAP)
    assert "设置" in answer and "LLM_API_KEY" in answer
    assert eng.last_source == "no-key"
    # A question the local router CAN answer still works without a key.
    local = eng.ask("我现在P几", SNAP)
    assert "P3" in local
    assert eng.last_source == "local"


def test_cancel_clears_between_asks():
    client = FakeClient()
    eng = Engineer(client=client, config=FakeConfig({}))
    eng.cancel()
    assert eng._cancel.is_set()
    eng.ask("我P几", SNAP)
    assert not eng._cancel.is_set()


class _Unconfigured:
    configured = False
    last_usage = None

    def chat(self, *a, **k):
        raise AssertionError("must not call LLM without a key")

    def describe(self):
        return {}


def test_voice_no_key_fast_answer_hits_local_router():
    """T1.1: voice mode with no key must still answer local questions.

    Previously voice_main short-circuited on ``not configured`` and never
    reached the local router, so "我P几" returned the no-key placeholder.
    """
    eng = Engineer(client=_Unconfigured(), config=FakeConfig({}))
    answer = eng.ask("我P几", SNAP, channel="voice")
    assert "P3" in answer
    assert eng.last_source == "local"


def test_voice_no_key_hint_points_at_config_cli():
    """T1.1: the voice no-key hint mentions ``FI.py --config``, not the web UI."""
    eng = Engineer(client=_Unconfigured(), config=FakeConfig({}))
    answer = eng.ask("帮我规划进站策略", SNAP, channel="voice")
    assert "FI.py --config" in answer
    assert "LLM_API_KEY" in answer
    assert eng.last_source == "no-key"
    # web keeps pointing at the settings panel
    web = eng.ask("帮我规划进站策略", SNAP, channel="web")
    assert "设置" in web


# --- T1.2: laps remaining vs current lap number ---

def test_laps_remaining_is_not_current_lap():
    eng = Engineer(client=FakeClient(), config=FakeConfig({}))
    # SNAP: current_lap 8, total_laps 15 -> 8 laps remaining (incl. current).
    answer = eng.ask("还剩几圈", SNAP)
    assert "还剩 8 圈" in answer, answer
    assert eng.last_source == "local"


def test_current_lap_still_answers_lap_number():
    eng = Engineer(client=FakeClient(), config=FakeConfig({}))
    answer = eng.ask("现在第几圈", SNAP)
    assert "第 8 圈" in answer, answer


def test_laps_remaining_without_lap_count_uses_session_clock():
    from profiles import LocalRouter
    facts = {"lap": 3, "laps_remaining": None, "session_time_left_s": 425}
    # laps_remaining absent -> fall back to the session clock.
    facts.pop("laps_remaining")
    out = LocalRouter().answer("还剩几圈", facts)
    assert out is not None and "7 分" in out.text, out


def test_gap_ahead_no_data_is_not_lead():
    """T1.2: missing gap must not be reported as leading the race."""
    from profiles import LocalRouter
    router = LocalRouter()
    assert router.answer("距前车多远", {"gap_to_front": None}) is None
    # An explicit "领跑" fact still reports leading.
    lead = router.answer("距前车多远", {"gap_to_front": "领跑"})
    assert lead is not None and "领跑" in lead.text
