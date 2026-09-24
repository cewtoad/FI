"""Regression test for ISSUE-4: gap-to-leader direction must be unambiguous.

Real data (session_20260922_004903): the player was P18, 19.982s behind the
leader, yet the model once answered "you lead by 2.4s". The data was correct;
the rendered text was ambiguous (a bare "19.982" under the key gap_to_leader).

These tests lock in the fix: the rendered summary must spell out "落后"
(behind) for a non-leader and never describe a positive gap as leading.
"""

from __future__ import annotations

from prompts import build_snapshot_text
from summariser import Summariser


def _snapshot_player_behind():
    """Player P18, 19.982s behind leader (values from the real session)."""
    return {
        "latest": {
            "lap": {
                "car_position": 18,
                "current_lap_num": 4,
                "current_lap_time_ms": None,
                "last_lap_time_ms": 86420,
                "delta_to_car_in_front_ms": 417,
                "delta_to_race_leader_ms": 19982,
                "sector1_ms": None,
                "sector2_ms": None,
                "sector3_ms": None,
                "pit_status": "NONE",
                "num_pit_stops": 0,
            },
            "car": {},
            "car2": {},
            "status": {},
        },
        "delta": None,
        "fuel": None,
        "trends": {},
        "leaderboard": [
            {
                "position": 1, "car_index": 13, "driver": "诺里斯", "is_player": False,
                "gap_to_leader_ms": 0, "gap_to_front_ms": 0, "tyre": "C4",
            },
            {
                "position": 18, "car_index": 19, "driver": "Mercedes #12", "is_player": True,
                "gap_to_leader_ms": 19982, "gap_to_front_ms": 417, "tyre": "C4",
            },
        ],
        "position_context": {
            "position": 18,
            "gap_to_leader_ms": 19982,
            "gap_to_front_ms": 417,
            "ahead": {"driver": "奥康", "position": 17, "gap_ms": 417},
            "behind": {"driver": "加斯利", "position": 19, "gap_ms": 250},
        },
        "events": [],
    }


def test_facts_gap_direction_is_explicit():
    facts = Summariser().summarise(_snapshot_player_behind())["facts"]
    assert facts["gap_to_leader"] == "落后 19.982s", facts["gap_to_leader"]
    assert facts["gap_to_front"] == "落后 0.417s", facts["gap_to_front"]


def test_leader_is_marked_leading_not_behind():
    snap = _snapshot_player_behind()
    snap["latest"]["lap"]["car_position"] = 1
    snap["latest"]["lap"]["delta_to_race_leader_ms"] = 0
    snap["latest"]["lap"]["delta_to_car_in_front_ms"] = 0
    facts = Summariser().summarise(snap)["facts"]
    assert facts["gap_to_leader"] == "领先全场"
    assert facts["gap_to_front"] == "领跑"


def test_snapshot_text_never_says_leading_for_positive_gap():
    summary = Summariser().summarise(_snapshot_player_behind())
    text = build_snapshot_text(
        summary["facts"], summary["notes"],
        summary["leaderboard"], summary["recent_events"])
    assert "落后 19.982s" in text
    assert "领先 19.982" not in text
    # The player row must be described as behind, not leading.
    assert "18.*Mercedes #12 C4 落后 20.0s" in text


def test_leaderboard_header_states_direction():
    summary = Summariser().summarise(_snapshot_player_behind())
    text = build_snapshot_text(
        summary["facts"], summary["notes"],
        summary["leaderboard"], summary["recent_events"])
    assert "落后领先者" in text
    assert "1. 诺里斯 C4 领先全场" in text


if __name__ == "__main__":
    test_facts_gap_direction_is_explicit()
    test_leader_is_marked_leading_not_behind()
    test_snapshot_text_never_says_leading_for_positive_gap()
    test_leaderboard_header_states_direction()
    print("gap direction tests passed")
