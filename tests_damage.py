"""Test damage + pit surface into summary and TXT report."""

from __future__ import annotations

import json

from summariser import Summariser
from report_txt import render

SNAP = {
    "session": {"session_uid": 1},
    "latest": {
        "lap": {"current_lap_num": 5, "car_position": 18, "pit_status": "PITTING",
                "num_pit_stops": 1, "current_lap_time_ms": 45000,
                "last_lap_time_ms": 86500, "sector1_ms": 30000, "sector2_ms": 20000,
                "sector3_ms": 0, "delta_to_car_in_front_ms": 12000,
                "delta_to_race_leader_ms": 20000},
        "car": {"speed_kph": 80, "gear": 3, "tyres_surface_temp_c": [80, 80, 80, 80]},
        "status": {"tyre_compound_actual": "C4", "tyres_age_laps": 0,
                   "fuel_in_tank_kg": 30.0, "pit_limiter": True},
        "damage": {
            "front_left_wing": 45, "front_right_wing": 60, "rear_wing": 10,
            "floor": 30, "sidepod": 5, "engine": 12, "gearbox": 3,
            "drs_fault": True, "ers_fault": False, "engine_blown": False,
            "tyre_wear_fl": 25.0, "tyre_wear_fr": 22.0, "tyre_wear_rl": 18.0,
            "tyre_wear_rr": 15.0, "worst_bodywork": 60, "has_significant_damage": True,
        },
    },
    "trends": {"lap_times_ms": [86500], "best_lap_ms": 86500},
    "delta": {"delta_ms": 100}, "leaderboard": [], "position_context": {},
}


def main():
    s = Summariser().summarise(SNAP)
    print("notes:")
    for n in s["notes"]:
        print("  -", n)
    print("\ndamage facts:", {k: v for k, v in s["facts"].items() if "damage" in k or "drs" in k or "tyre_wear" in k or "pit" in k})
    assert any("车损" in n for n in s["notes"]), s["notes"]
    assert any("进站" in n or "限速" in n for n in s["notes"]), s["notes"]
    assert s["facts"].get("pit_stops") == 1

    # TXT report
    data = {"started": "t", "track": "Melbourne", "session_type": "Race",
            "laps": [{"lap_num": 1, "lap_time_ms": 86500, "sector1_ms": 30000,
                      "sector2_ms": 20000, "sector3_ms": 36500, "valid": True,
                      "tyre_compound": "C4", "tyres_age_laps": 1}],
            "final_leaderboard": [], "qa": [],
            "vehicle_status": {"damage": SNAP["latest"]["damage"],
                               "pit_status": "PITTING", "num_pit_stops": 1,
                               "pit_limiter": True}}
    txt = render(data)
    print("\n--- TXT ---")
    print(txt)
    assert "车损" in txt and "进站次数" in txt
    print("\nDAMAGE+PIT OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
