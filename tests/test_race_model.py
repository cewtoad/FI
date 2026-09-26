"""T4: race model trends/predictions/windows."""

from __future__ import annotations

import time

from race_model import RaceModel


def _snap(samples, **kw):
    base = {
        "session": {"session_kind": "race", "total_laps": 50, "track_length_m": 5000,
                    "pit_window_ideal_lap": kw.get("ideal", 0),
                    "pit_window_latest_lap": kw.get("latest", 0),
                    "pit_window_rejoin_position": kw.get("rejoin", 0),
                    "safety_car_status": kw.get("sc", "NO SAFETY CAR"),
                    "weather_forecast": kw.get("forecast", []),
                    "marshal_yellow_zones": kw.get("yellow", [])},
        "latest": {"lap": {"current_lap_num": kw.get("cur", len(samples)),
                           "lap_distance_m": kw.get("dist", 0.0),
                           "pit_status": "NONE"},
                   "status": {}},
        "fuel": {"surplus_laps": kw.get("surplus", 1.0)},
        "lap_snapshots": samples,
        "leaderboard": kw.get("leaderboard", []),
    }
    return base


def _lap(n, **kw):
    d = {"lap_num": n, "lap_time_ms": 85000, "valid": True,
         "tyre_compound": "C3", "tyre_age_laps": n, "tyre_wear_max_pct": n * 2.0,
         "fuel_kg": 50 - n, "fuel_burn_kg": 1.5,
         "position": 3, "gap_ahead_ms": 2000, "ahead_index": 1,
         "gap_behind_ms": 3000, "behind_index": 4,
         "safety_car": "NO SAFETY CAR", "pit_this_lap": False}
    d.update(kw)
    return d


def test_wear_regression_and_projected_life():
    samples = [_lap(n) for n in range(1, 7)]  # wear 2%/lap
    m = RaceModel()
    m.update(_snap(samples, cur=6), now=0.0)
    st = m.latest.stint
    assert st is not None
    assert abs(st.wear_rate_pct_per_lap - 2.0) < 0.01, st
    assert abs(st.wear_now_pct - 12.0) < 0.01
    # (70 - 12) / 2 = 29 laps
    assert abs(st.projected_life_laps - 29.0) < 0.5, st


def test_pace_degradation():
    samples = [_lap(n, lap_time_ms=85000 + (n - 1) * 100) for n in range(1, 6)]
    m = RaceModel()
    m.update(_snap(samples, cur=5), now=0.0)
    # +100ms per lap = +0.1 s/lap
    assert abs(m.latest.stint.pace_degradation_s_per_lap - 0.1) < 0.01


def test_gap_closing_rate_and_laps_to_1s():
    samples = [_lap(n, gap_ahead_ms=3000 - n * 500, ahead_index=1)
               for n in range(1, 5)]   # closes 500ms/lap
    m = RaceModel()
    m.update(_snap(samples, cur=4), now=0.0)
    g = m.latest.ahead
    assert g is not None and g.gap_ms_now == 1000
    assert g.closing_rate_ms_per_lap < 0
    assert g.laps_to_1s is None  # already at 1s (gap==1000, not >1000)


def test_gap_resets_when_opponent_changes():
    samples = [_lap(1, gap_ahead_ms=2000, ahead_index=1),
               _lap(2, gap_ahead_ms=1800, ahead_index=1),
               _lap(3, gap_ahead_ms=5000, ahead_index=2),   # new opponent
               _lap(4, gap_ahead_ms=4800, ahead_index=2)]
    m = RaceModel()
    m.update(_snap(samples, cur=4), now=0.0)
    assert m.latest.ahead.target_index == 2
    assert m.latest.ahead.gap_ms_now == 4800


def test_pit_window_state_machine():
    m = RaceModel()
    for cur, expect in ((3, "not_open"), (5, "open"), (7, "last_lap"),
                        (8, "missed")):
        m.update(_snap([_lap(1)], cur=cur, ideal=5, latest=7), now=0.0)
        assert m.latest.pit_window.state == expect, (cur, m.latest.pit_window)


def test_pit_window_unknown_without_ideal():
    m = RaceModel()
    m.update(_snap([_lap(1)], cur=3, ideal=0), now=0.0)
    assert m.latest.pit_window.state == "unknown"


def test_rain_eta_and_field_best():
    forecast = [{"time_offset_min": 0, "rain_pct": 0},
                {"time_offset_min": 8, "rain_pct": 60}]
    lb = [{"best_lap_ms": 84000}, {"best_lap_ms": 82000}]
    m = RaceModel()
    m.update(_snap([_lap(1)], cur=1, forecast=forecast, leaderboard=lb), now=0.0)
    assert m.latest.rain_eta_min == 8.0
    assert m.latest.field_best_lap_ms == 82000
    assert m.latest.pole_lap_ms == 82000


def test_fuel_laps_left():
    m = RaceModel()
    m.update(_snap([_lap(1)], cur=10, surplus=2.0), now=0.0)
    # total 50 - cur 10 + 1 = 41: the SAME laps-remaining convention as the
    # summariser's facts.laps_remaining (includes the lap being driven), so
    # the LLM never sees two "laps left" numbers that disagree by one.
    assert m.latest.fuel_laps_left == 41


def test_update_is_under_2ms():
    samples = [_lap(n) for n in range(1, 60)]
    m = RaceModel()
    snap = _snap(samples, cur=59)
    # warm up
    m.update(snap, now=0.0)
    t0 = time.perf_counter()
    for _ in range(50):
        m.update(snap, now=0.0)
    dt_ms = (time.perf_counter() - t0) / 50 * 1000
    assert dt_ms < 2.0, f"race model update took {dt_ms:.3f}ms"


def test_latest_dict_is_serialisable():
    import json
    m = RaceModel()
    m.update(_snap([_lap(1), _lap(2)], cur=2), now=0.0)
    d = m.latest_dict()
    json.dumps(d, default=str)  # must not raise
    assert d["session_kind"] == "race"
