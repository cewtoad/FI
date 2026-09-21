"""Leaderboard test: build a 24-car field, verify ranking + battle context + AI prompt."""

from __future__ import annotations

import struct

from lib.f1_types import (F1PacketType, PacketHeader, PacketLapData, LapData,
                          PacketParticipantsData)
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState
from summariser import Summariser
from prompts import build_snapshot_text
from lib.f1_types.packet_4_participants_data import ParticipantData

FMT = 2026
N = 24
PLAYER = 3   # player is car index 3, racing in P2


def hdr(pid, uid, frame, t):
    return PacketHeader.from_values(FMT, 26, 1, 0, 1, pid, uid, t, frame, frame,
                                    PLAYER, 255)


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)
    uid = 7

    # Build cars: positions 1..24, player at P2 (car index 3).
    order = list(range(1, N + 1))  # car index i -> position mapping below
    laps = []
    for i in range(N):
        # assign positions so that car index 3 is P2
        pos = i + 1
        if i == PLAYER:
            pos = 2
        elif i == 0:
            pos = 1
        elif i == 1:
            pos = 3
        laps.append(LapData.from_values(
            last_lap_time_ms=82000 + i * 100, current_lap_time_ms=20000 + i * 50,
            sector1_time_ms=28000, sector1_time_minutes=0,
            sector2_time_ms=18000, sector2_time_minutes=0,
            delta_to_front_ms=0 if pos == 1 else 1000 + i * 10,
            delta_to_front_minutes=0,
            delta_to_leader_ms=0 if pos == 1 else pos * 1000,
            delta_to_leader_minutes=0,
            lap_distance=2000.0, total_distance=20000.0, safety_car_delta=0.0,
            car_position=pos, current_lap_num=5, pit_status=0, num_pit_stops=0,
            sector=0, current_lap_invalid=0, penalties=0, total_warnings=0,
            corner_cutting_warnings=0, num_unserved_drive_through_pens=0,
            num_unserved_stop_go_pens=0, grid_position=pos, driver_status=4,
            result_status=2, pit_lane_timer_active=0, pit_lane_time_ms=0,
            pit_stop_timer_ms=0, pit_stop_should_serve_pen=0,
            speed_trap_fastest_speed=300.0, speed_trap_fastest_lap=1,
            packet_format=FMT))
    raw = PacketLapData.from_values(hdr(F1PacketType.LAP_DATA, uid, 1, 5.0), laps).to_bytes()
    pkt = factory.parse(raw)
    assert pkt is not None, factory.last_failure_reason
    state.process(pkt)

    snap = state.snapshot()
    lb = snap.get("leaderboard", [])
    print("leaderboard rows:", len(lb))
    for r in lb[:5]:
        mark = "*" if r["is_player"] else " "
        print(f"  {r['position']}.{mark}{r['driver']:10} gap={r['gap_to_leader_ms']}ms tyre={r['tyre']}")

    assert len(lb) == N, len(lb)
    assert lb[0]["position"] == 1
    assert lb[1]["is_player"], "player should be P2"

    pc = snap.get("position_context")
    print("\nposition_context:", pc)
    assert pc["position"] == 2
    assert pc["ahead"]["position"] == 1

    summary = Summariser().summarise(snap)
    print("\nnotes:", summary["notes"])
    assert "car_ahead" in summary["facts"]

    text = build_snapshot_text(summary["facts"], summary["notes"], summary["leaderboard"])
    print("\n--- PROMPT SNAPSHOT ---")
    print(text)
    print("\nLEADERBOARD OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
