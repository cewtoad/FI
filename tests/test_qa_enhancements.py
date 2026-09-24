"""T9: Q&A enhancement - namespaced facts, intent selection, prompt discipline."""

from __future__ import annotations

from engineer import Engineer
from profiles import LocalRouter
from prompts import SYSTEM_PROMPT, _select_facts, build_messages


class FakeClient:
    configured = True
    last_usage = None

    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.3, max_tokens=512):
        self.calls.append(messages)
        return "ok"

    def describe(self):
        return {}


class FakeConfig:
    def get(self, key, default=""):
        return default

    def get_int(self, key, default):
        return default

    def get_float(self, key, default):
        return default


FACTS = {
    "lap": 10, "total_laps": 50, "position": 3,
    "last_lap_time": "1:24.500", "best_lap_time": "1:23.900",
    "tyre_compound": "C3", "tyre_age_laps": 8,
    "stint.compound": "C3", "stint.wear_pct": 40.0,
    "stint.wear_rate_pct_per_lap": 2.5, "stint.tyre_laps_to_limit": 12.0,
    "pace.degradation_s_per_lap": 0.08,
    "gap.ahead_ms": 1500, "gap.behind_ms": 3000,
    "pit.window_state": "open", "pit.ideal_lap": 15, "pit.latest_lap": 18,
    "pit.rejoin_position": 5,
    "fuel.laps_left": 40, "weather.rain_eta_min": 8,
    "qualifying.field_best": "1:22.000",
}


def test_select_facts_tyre_intent():
    out = _select_facts("我的轮胎还能撑几圈", FACTS)
    assert "stint.tyre_laps_to_limit" in out
    assert "gap.ahead_ms" not in out
    assert "lap" in out  # core kept


def test_select_facts_gap_intent():
    out = _select_facts("前面差距多少", FACTS)
    assert "gap.ahead_ms" in out
    assert "stint.wear_rate_pct_per_lap" not in out


def test_select_facts_strategy_intent():
    out = _select_facts("进站策略怎么安排", FACTS)
    assert "pit.window_state" in out and "fuel.laps_left" in out
    assert "weather.rain_eta_min" not in out


def test_select_facts_no_match_returns_all():
    out = _select_facts("你好", FACTS)
    assert out == FACTS


def test_system_prompt_forbids_pit_instruction():
    assert "不得给出进站指令" in SYSTEM_PROMPT
    assert "祈使句" in SYSTEM_PROMPT


def test_build_messages_snapshot_is_namespaced():
    summary = {"facts": FACTS, "notes": [], "leaderboard": [], "recent_events": []}
    msgs = build_messages("我的轮胎状况", summary)
    content = msgs[-1]["content"]
    assert "stint.wear_pct" in content
    assert "gap.ahead_ms" not in content


def test_new_fast_answer_intents():
    r = LocalRouter()
    facts = {"stint.tyre_laps_to_limit": 9.0,
             "pit.window_state": "open", "pit.ideal_lap": 15, "pit.latest_lap": 18}
    a = r.answer("轮胎还能跑几圈", facts)
    assert a and a.intent == "tyre_life" and "9" in a.text
    b = r.answer("进站窗口开了吗", facts)
    assert b and b.intent == "pit_window" and "开" in b.text


def test_rival_pace_route_by_name():
    from names import NameRenderer
    router = LocalRouter()
    lb = [{"driver": "LANDO NORRIS", "last_lap_ms": 83210}]
    ans = router.answer("诺里斯圈速多少", {}, leaderboard=lb,
                        name_renderer=NameRenderer(style="zh"))
    assert ans is not None and ans.intent == "rival_pace"
    assert "1:23.210" in ans.text


def test_no_key_fast_answer_still_hits_new_intents():
    class Unconfigured:
        configured = False
        last_usage = None

        def chat(self, *a, **k):
            raise AssertionError("no LLM without key")

        def describe(self):
            return {}

    eng = Engineer(client=Unconfigured(), config=FakeConfig())
    snap = {"latest": {}, "session": {}, "delta": {}, "fuel": {},
            "trends": {}, "leaderboard": [], "events": [],
            "race_model": {"tyre_laps_to_limit": 8.0,
                           "pit_window": {"state": "open", "ideal_lap": 15,
                                          "latest_lap": 18, "rejoin_position": 5}}}
    ans = eng.ask("轮胎还能跑几圈", snap)
    assert eng.last_source == "local" and "8" in ans
