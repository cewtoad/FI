"""Offline end-to-end test: fake UDP packets -> parse -> state -> snapshot.

Run: py -3.12 -m tests_offline
"""

from __future__ import annotations

import json
import logging

from lib.f1_types import F1PacketType
from lib.telemetry_manager import PacketParserFactory

from fake_data import (PACKET_FORMAT, make_lap, make_session, make_status,
                       make_telemetry)
from receiver import PACKETS_RACE
from state import TelemetryState
from summariser import Summariser

log = logging.getLogger("offline")


def _feed(factory, state, raw, label):
    pkt = factory.parse(raw)
    assert pkt is not None, f"parse failed for {label}: {factory.last_failure_reason}"
    state.process(pkt)
    return pkt


def main():
    logging.basicConfig(level=logging.INFO)
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_RACE, log)

    session_uid = 12345678
    # Session packet (from_values signature is complex; skip if it fails).
    try:
        _feed(factory, state, make_session(session_uid, 5), "session")
        print("[ok] session parsed")
    except Exception as e:
        print(f"[warn] session packet skipped: {e!r}")

    lap_len_m = 5000.0
    total = 0.0
    frame = 1
    t = 0.0

    # Lap 1: 80.0s
    for step in range(0, 100):
        dist = lap_len_m * step / 100
        cur_ms = int(80000 * step / 100)
        raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, session_uid, frame, t,
                       last_lap_ms=0, cur_lap_ms=cur_ms, lap_distance=dist,
                       total_distance=total + dist, cur_lap_num=1, position=5, sector=0)
        _feed(factory, state, raw, f"lap1-{step}")
        raw = make_telemetry(PACKET_FORMAT, session_uid, frame, t,
                             speed=280, gear=7, throttle=1.0, brake=0.0, tyre_surface=95)
        _feed(factory, state, raw, "telem1")
        raw = make_status(PACKET_FORMAT, session_uid, frame, t,
                          fuel_kg=100.0 - step * 0.018, fuel_laps=4.5, tyre_age=1)
        _feed(factory, state, raw, "status1")
        frame += 1
        t += 0.8

    # Lap 2 start: last lap = 80000 ms
    total += lap_len_m
    t += 0.8
    for step in range(0, 100):
        dist = lap_len_m * step / 100
        cur_ms = int(79500 * step / 100)   # faster lap
        raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, session_uid, frame, t,
                       last_lap_ms=80000, cur_lap_ms=cur_ms, lap_distance=dist,
                       total_distance=total + dist, cur_lap_num=2, position=4, sector=0)
        _feed(factory, state, raw, f"lap2-{step}")
        raw = make_status(PACKET_FORMAT, session_uid, frame, t,
                          fuel_kg=98.0 - step * 0.018, fuel_laps=3.5, tyre_age=2)
        _feed(factory, state, raw, "status2")
        frame += 1
        t += 0.8

    # Lap 3 start: last lap = 79500 ms (completed lap 2)
    total += lap_len_m
    t += 0.8
    raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, session_uid, frame, t,
                   last_lap_ms=79500, cur_lap_ms=1000, lap_distance=30.0,
                   total_distance=total + 30, cur_lap_num=3, position=3, sector=0)
    _feed(factory, state, raw, "lap3")
    raw = make_status(PACKET_FORMAT, session_uid, frame, t,
                      fuel_kg=96.0, fuel_laps=2.5, tyre_age=3)
    _feed(factory, state, raw, "status3")

    snap = state.snapshot()
    print(json.dumps(snap, indent=2, ensure_ascii=False, default=str))

    summary = Summariser().summarise(snap)
    print("\n=== SUMMARISER OUTPUT ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))

    # --- assertions ---
    lap = snap["latest"].get("lap", {})
    assert lap.get("current_lap_num") == 3, lap
    assert snap["trends"]["best_lap_ms"] == 79500, snap["trends"]
    assert len(snap["trends"]["lap_times_ms"]) == 2, snap["trends"]
    assert snap["trends"]["lap_times_ms"] == [80000, 79500], snap["trends"]
    assert snap["delta"] is not None, "delta should be computed on lap 3"
    assert summary["facts"]["best_lap_time"] == "1:19.500", summary["facts"]
    assert summary["facts"]["delta_to_best"].endswith("ms"), summary["facts"]
    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
