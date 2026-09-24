"""Test F1 25 2026 Season Pack format (packetFormat=2026, 24 cars, packet 16).

Run: py -3.12 tests_2026.py
"""

from __future__ import annotations

import struct

from lib.f1_types import (ActualTyreCompound, F1PacketType, PacketCarStatusData,
                          PacketCarTelemetryData, PacketHeader, PacketLapData,
                          CarStatusData, CarTelemetryData, LapData,
                          TractionControlAssistMode)
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_RACE
from state import TelemetryState
from summariser import Summariser

FMT = 2026
NUM_CARS = 24
PLAYER = 0


def header(pid: F1PacketType, uid: int, frame: int, t: float) -> PacketHeader:
    return PacketHeader.from_values(
        packet_format=FMT, game_year=26, game_major_version=1, game_minor_version=0,
        packet_version=1, packet_type=pid, session_uid=uid, session_time=t,
        frame_identifier=frame, overall_frame_identifier=frame,
        player_car_index=PLAYER, secondary_player_car_index=255)


def lap_pkt(uid, frame, t, cur_ms, dist, lap_num, pos) -> bytes:
    laps = [LapData.from_values(
        last_lap_time_ms=0, current_lap_time_ms=cur_ms,
        sector1_time_ms=0, sector1_time_minutes=0,
        sector2_time_ms=0, sector2_time_minutes=0,
        delta_to_front_ms=800, delta_to_front_minutes=0,
        delta_to_leader_ms=3000, delta_to_leader_minutes=0,
        lap_distance=dist, total_distance=dist, safety_car_delta=0.0,
        car_position=pos, current_lap_num=lap_num, pit_status=0, num_pit_stops=0,
        sector=0, current_lap_invalid=0, penalties=0, total_warnings=0,
        corner_cutting_warnings=0, num_unserved_drive_through_pens=0,
        num_unserved_stop_go_pens=0, grid_position=pos, driver_status=4,
        result_status=2, pit_lane_timer_active=0, pit_lane_time_ms=0,
        pit_stop_timer_ms=0, pit_stop_should_serve_pen=0,
        speed_trap_fastest_speed=300.0, speed_trap_fastest_lap=1,
        packet_format=FMT) for _ in range(NUM_CARS)]
    return PacketLapData.from_values(header(F1PacketType.LAP_DATA, uid, frame, t), laps).to_bytes()


def telemetry2_pkt(uid, frame, t, aero_mode, overtake_active) -> bytes:
    # CarTelemetry2Data has no from_values; build bytes manually.
    entry = struct.Struct("<BBHBBHBB")
    blob = b"".join(entry.pack(aero_mode, 1, 50, 1, 1 if overtake_active else 0, 80, 1, 0)
                    for _ in range(NUM_CARS))
    return header(F1PacketType.CAR_TELEMETRY_2, uid, frame, t).to_bytes() + blob


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_RACE, None)
    uid = 999
    frame = 1

    # Feed a lap packet (2026 -> 24 cars) and a packet-16 (active aero + overtake).
    for step in range(30):
        dist = 5000 * step / 30
        cur = int(80000 * step / 30)
        raw = lap_pkt(uid, frame, step * 0.8, cur, dist, 1, 5)
        pkt = factory.parse(raw)
        assert pkt is not None, factory.last_failure_reason
        state.process(pkt)
        raw2 = telemetry2_pkt(uid, frame, step * 0.8, 1, True)
        pkt2 = factory.parse(raw2)
        assert pkt2 is not None, factory.last_failure_reason
        state.process(pkt2)
        frame += 1

    snap = state.snapshot()
    print("packet_counts:", snap["packet_counts"])
    print("packet_errors:", snap["packet_errors"])
    print("lap:", snap["latest"].get("lap", {}).get("current_lap_num"))
    print("car2:", snap["latest"].get("car2"))

    assert snap["packet_counts"].get("LAP_DATA") == 30
    assert snap["packet_counts"].get("CAR_TELEMETRY_2") == 30
    assert not snap["packet_errors"], f"unexpected errors: {snap['packet_errors']}"
    car2 = snap["latest"]["car2"]
    assert car2["active_aero_mode"] == "STRAIGHT_MODE", car2
    assert car2["overtake_active"] is True
    assert car2["regulations_2026"] is True

    summary = Summariser().summarise(snap)
    assert summary["facts"]["active_aero"] == "STRAIGHT_MODE"
    assert any("Overtake" in n for n in summary["notes"]), summary["notes"]
    print("\n2026 FORMAT OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()


# --- pytest entry point (T0: script-style -> pytest) ---

import asyncio as _asyncio
import inspect as _inspect


def test_main() -> None:
    """Run this module's script-style ``main()`` under pytest."""
    result = main()
    if _inspect.isawaitable(result):
        _asyncio.run(result)


# --- T1.5: 2026 regs hide DRS in the summary ---

def _snap_2026(regs: bool, drs_fault: bool = False, drs_allowed: int = 1) -> dict:
    return {
        "latest": {
            "car": {},
            "car2": {"regulations_2026": regs, "overtake_available": True},
            "status": {"drs_allowed": drs_allowed},
            "damage": {"drs_fault": drs_fault},
        },
        "session": {}, "delta": {}, "fuel": {}, "trends": {},
        "leaderboard": [], "events": [],
    }


def test_2026_hides_drs_facts_and_notes():
    s = Summariser(config=None).summarise(_snap_2026(regs=True, drs_fault=True))
    assert "drs_allowed" not in s["facts"]
    assert "drs_fault" not in s["facts"]
    assert not any("DRS" in n for n in s["notes"]), s["notes"]
    # Overtake is still surfaced as the 2026 equivalent.
    assert s["facts"].get("overtake_available") is True


def test_pre_2026_keeps_drs_facts():
    s = Summariser(config=None).summarise(_snap_2026(regs=False, drs_fault=True))
    assert s["facts"].get("drs_allowed") == 1
    assert s["facts"].get("drs_fault") is True
    assert any("DRS" in n for n in s["notes"]), s["notes"]
