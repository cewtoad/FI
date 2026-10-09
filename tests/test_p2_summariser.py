"""P2: intent-sliced summariser facts, trend lines, SuggestedAction (no hardware)."""

from __future__ import annotations

from contracts import SuggestedAction
from prompts import _select_facts, build_messages, build_snapshot_text
from summariser import (
    Summariser,
    build_suggested_actions,
    dedupe_notes,
    detect_intents,
    race_model_trend_lines,
    slice_facts,
)
from telemetry_source import UdpTelemetrySource, make_telemetry_source
from timefmt import fmt_gap_signed, fmt_ms


def _snap(**extra):
    base = {
        "latest": {
            "lap": {
                "car_position": 4,
                "current_lap_num": 12,
                "current_lap_time_ms": 50000,
                "last_lap_time_ms": 84000,
                "delta_to_car_in_front_ms": 800,
                "delta_to_race_leader_ms": 5200,
                "sector1_ms": 28000,
                "sector2_ms": None,
                "sector3_ms": None,
                "pit_status": "NONE",
                "num_pit_stops": 0,
            },
            "car": {"speed_kph": 280, "tyres_inner_temp_median_c": [90, 91, 89, 90]},
            "car2": {"overtake_available": True, "overtake_active": False,
                     "regulations_2026": True},
            "status": {"tyre_compound_actual": "C3", "tyres_age_laps": 8,
                       "fuel_in_tank_kg": 22.0, "fuel_remaining_laps": 18.0},
            "session": {"total_laps": 50, "rain_percentage": 10},
            "damage": {},
        },
        "session": {"total_laps": 50},
        "delta": {"delta_ms": 200},
        "fuel": {"surplus_laps": -0.5, "curr_fuel_rate_kg_per_lap": 1.7,
                 "predicted_final_fuel_kg": -0.8},
        "trends": {"best_lap_ms": 83000, "lap_times_ms": [84000, 84500]},
        "race_model": {
            "stint": {"compound": "C3", "laps": 8, "wear_now_pct": 35.0,
                      "wear_rate_pct_per_lap": 2.0,
                      "pace_degradation_s_per_lap": 0.05},
            "tyre_laps_to_limit": 12.0,
            "ahead": {"gap_ms_now": 800, "closing_rate_ms_per_lap": 120.0,
                      "laps_to_1s": 1.5},
            "behind": {"gap_ms_now": 1500},
            "pit_window": {"state": "open", "ideal_lap": 14, "latest_lap": 17,
                          "rejoin_position": 6},
            "rain_eta_min": None,
            "field_best_lap_ms": 82500,
        },
        "position_context": {
            "ahead": {"driver": "A", "position": 3, "gap_ms": 800},
            "behind": {"driver": "B", "position": 5, "gap_ms": 1500},
        },
        "leaderboard": [],
        "events": [],
    }
    base.update(extra)
    return base


def test_timefmt_shared():
    assert fmt_ms(83055) == "1:23.055"
    assert fmt_gap_signed(19982) == "落后 19.982s"


def test_detect_and_slice_intents():
    assert "tyres" in detect_intents("轮胎磨损怎么样")
    assert "gaps" in detect_intents("前车差距")
    assert "fuel" in detect_intents("油量还够吗") or "pit" in detect_intents("油量还够吗")
    assert "weather" in detect_intents("会下雨吗")
    facts = {
        "lap": 1, "position": 2, "tyre_compound": "C3",
        "stint.wear_pct": 40, "gap.ahead_ms": 100,
        "fuel_kg": 20, "pit.window_state": "open",
        "weather.rain_eta_min": 5, "noise": 9,
    }
    tyre = slice_facts(facts, intents=["tyres"])
    assert "stint.wear_pct" in tyre and "gap.ahead_ms" not in tyre and "lap" in tyre
    gap = slice_facts(facts, intents=["gaps"])
    assert "gap.ahead_ms" in gap and "stint.wear_pct" not in gap
    assert slice_facts(facts, question="你好") == facts


def test_select_facts_delegates():
    facts = {"lap": 1, "stint.wear_pct": 10, "gap.ahead_ms": 1}
    out = _select_facts("我的轮胎", facts)
    assert "stint.wear_pct" in out and "gap.ahead_ms" not in out


def test_dedupe_notes():
    assert dedupe_notes(["a", "a", "  a  ", "b", ""]) == ["a", "b"]


def test_trend_lines_and_suggested_actions():
    snap = _snap()
    lines = race_model_trend_lines(snap)
    assert any("衰退" in x for x in lines)
    assert any("进站窗口" in x for x in lines)
    actions = build_suggested_actions(snap)
    assert any(a.suggested_key_name == "进站确认" for a in actions)
    assert any(a.suggested_key_name == "Overtake" for a in actions)
    # Never looks like a hid:/xi:/kb: binding.
    for a in actions:
        assert ":" not in a.suggested_key_name or a.suggested_key_name.startswith("DRS")
        assert not a.suggested_key_name.lower().startswith(("hid:", "xi:", "kb:", "hat:"))


def test_summarise_includes_p2_fields():
    summary = Summariser(config=None).summarise(_snap())
    assert "trend_lines" in summary and summary["trend_lines"]
    assert "suggested_actions" in summary
    assert any(a["suggested_key_name"] == "进站确认"
               for a in summary["suggested_actions"])
    assert summary["facts"]["gap_to_front"].startswith("落后")


def test_suggested_action_contract():
    a = SuggestedAction(text="t", suggested_key_name="进站确认")
    d = a.to_dict()
    assert d == {"text": "t", "suggested_key_name": "进站确认"}


def test_build_messages_includes_trends_and_advise_banner():
    summary = Summariser(config=None).summarise(_snap())
    msgs = build_messages("进站窗口开了吗", summary)
    content = msgs[-1]["content"]
    assert "【推演】" in content or any("进站窗口" in t for t in summary["trend_lines"])
    assert "【建议动作】" in content
    assert "程序不会代按" in content
    assert "进站确认" in content


def test_snapshot_text_advise_banner_standalone():
    text = build_snapshot_text(
        {"lap": 1}, [],
        suggested_actions=[{"text": "自行进站", "suggested_key_name": "进站确认"}])
    assert "【建议动作】" in text and "进站确认" in text and "不会代按" in text


def test_telemetry_source_udp_factory():
    class _R:
        async def run(self):
            return None

        def stats(self):
            return {"accepted": 0}

    src = make_telemetry_source("udp", receiver=_R())
    assert isinstance(src, UdpTelemetrySource)
    assert src.stats()["accepted"] == 0
    try:
        make_telemetry_source("memory")
        assert False, "memory source must raise"
    except ValueError as e:
        assert "udp" in str(e).lower()


def test_wheel_vid_describe_and_capture_config():
    from inputs.bindings import WHEEL_VIDS, describe_binding
    from inputs.hid import CaptureScan
    assert 0x046D in WHEEL_VIDS
    name = describe_binding("hid:046D:C24F:8:0x01", "zh")
    assert "方向盘" in name
    scan = CaptureScan()
    cfg = scan._config_for("\\\\?\\HID#VID_046D&PID_C24F#")
    assert cfg.get("baseline_s") == 0.6
