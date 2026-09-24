"""T1.4: per-lap fuel burn and tyre wear are actually written to their
RollingHistories (previously declared but never pushed).
"""

from __future__ import annotations

from state import TelemetryState


def test_fuel_and_wear_per_lap_are_recorded():
    st = TelemetryState()
    # Simulate end-of-lap fuel readings: lap 1 -> 48kg, lap 2 -> 46kg, lap 3 -> 44kg.
    st._fuel_at_lap_end = {1: 48.0, 2: 46.0, 3: 44.0}
    # A damage packet has populated current tyre wear.
    st.latest["damage"] = {
        "tyre_wear_fl": 20.0, "tyre_wear_fr": 22.5,
        "tyre_wear_rl": 18.0, "tyre_wear_rr": 19.0,
    }

    # Complete laps 1, 2 and 3 (each lap time valid).
    st._on_lap_completed(1, 83000, valid=True)
    st._on_lap_completed(2, 83200, valid=True)
    st._on_lap_completed(3, 83400, valid=True)

    fuel = st.fuel_per_lap.values()
    wear = st.tyre_wear_per_lap.values()
    # Lap 1 has no previous lap -> no burn recorded. Laps 2 & 3 each 2kg.
    assert fuel == [2.0, 2.0], fuel
    assert wear == [22.5, 22.5, 22.5], wear


def test_fuel_per_lap_skips_when_previous_missing():
    st = TelemetryState()
    st._fuel_at_lap_end = {5: 40.0}  # no lap-4 reading
    st._on_lap_completed(5, 83000, valid=True)
    assert st.fuel_per_lap.values() == []


def test_snapshot_exposes_new_trends():
    st = TelemetryState()
    st._fuel_at_lap_end = {1: 48.0, 2: 46.0}
    st.latest["damage"] = {"tyre_wear_rr": 30.0}
    st._on_lap_completed(2, 83000, valid=True)
    snap = st.snapshot()
    assert snap["trends"]["fuel_per_lap_kg"] == [2.0]
    assert snap["trends"]["tyre_wear_per_lap_pct"] == [30.0]


def test_history_laps_raised_to_100():
    assert TelemetryState.HISTORY_LAPS == 100
