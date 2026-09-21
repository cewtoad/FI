"""Test the engineer Q&A against a realistic snapshot. Run: py -3.12 tests_engineer.py"""

from __future__ import annotations

import sys

from engineer import Engineer

SNAP = {
    "session": {"session_uid": 1, "session_type": "Race", "track_id": "Melbourne",
                "total_laps": 15, "player_car_index": 0},
    "latest": {
        "lap": {"current_lap_num": 8, "current_lap_time_ms": 45231, "last_lap_time_ms": 83055,
                "sector1_ms": 28763, "sector2_ms": 18621, "sector3_ms": 0,
                "car_position": 1, "grid_position": 6,
                "delta_to_car_in_front_ms": 0, "delta_to_race_leader_ms": 0,
                "lap_distance_m": 2500.0, "total_distance_m": 37030.0,
                "pit_status": "NONE", "driver_status": "ON_TRACK",
                "current_lap_invalid": False, "num_pit_stops": 0},
        "car": {"speed_kph": 271, "gear": 7, "rpm": 11478, "throttle": 1.0, "brake": 0.0,
                "steer": -0.02, "drs": False,
                "tyres_surface_temp_c": [60, 57, 51, 45],
                "tyres_inner_temp_c": [86, 85, 83, 82],
                "tyres_pressure_psi": [21.4, 21.3, 24.1, 24.0], "engine_temp_c": 116},
        "status": {"fuel_in_tank_kg": 17.86, "fuel_capacity_kg": 110.0, "fuel_remaining_laps": 2.22,
                   "tyre_compound_actual": "C4", "tyre_compound_visual": "Medium", "tyres_age_laps": 7,
                   "ers_store_energy_j": 3692124.0, "ers_deploy_mode": "Medium", "drs_allowed": 0,
                   "pit_limiter": False},
        "car2": {"active_aero_mode": "CORNER_MODE", "overtake_available": False, "overtake_active": False},
        "history": {"num_laps": 8, "laps": [
            {"lap_num": 5, "lap_time_ms": 85304, "sector1_ms": 29695, "sector2_ms": 18716, "sector3_ms": 36891, "valid": True},
            {"lap_num": 6, "lap_time_ms": 83541, "sector1_ms": 29014, "sector2_ms": 18730, "sector3_ms": 35796, "valid": True},
            {"lap_num": 7, "lap_time_ms": 83055, "sector1_ms": 28763, "sector2_ms": 18621, "sector3_ms": 35670, "valid": True}]},
    },
    "delta": {"delta_ms": 49, "best_lap_num": 7, "distance_m": 2500.0},
    "fuel": {"curr_fuel_rate_kg_per_lap": 1.793, "target_fuel_rate_kg_per_lap": 2.046,
             "fuel_used_last_lap_kg": 1.797, "surplus_laps": 1.129,
             "predicted_final_fuel_kg": 3.524, "data_sufficient": True},
    "trends": {"lap_times_ms": [85304, 83541, 83055], "best_lap_ms": 83055},
    "packet_counts": {}, "packet_errors": {},
}


def main():
    eng = Engineer()
    if not eng.configured:
        print("key not configured"); return 2
    questions = [
        "我圈速多少",
        "轮胎怎么样",
        "油量够跑完吗",
        "我排第几",
        "我哪里损失了时间",
        "跟红牛差多少",  # 数据里没有 -> 应该承认没有
    ]
    for q in questions:
        print(f"\nQ: {q}")
        try:
            a = eng.ask(q, SNAP)
            print(f"A: {a}")
            if eng.client.last_usage:
                u = eng.client.last_usage
                print(f"   [tokens total={u.get('total_tokens')} reasoning={u.get('completion_tokens_details',{}).get('reasoning_tokens')}]")
        except Exception as e:
            print(f"ERR: {e}")
    print("\nENGINEER TEST DONE")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
