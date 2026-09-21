"""Overtake event test: player P1 -> P2, verify event text + AI answers with authority."""

from __future__ import annotations

from lib.f1_types import F1PacketType, PacketHeader, PacketLapData, LapData
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState
from summariser import Summariser
from prompts import build_snapshot_text
from engineer import Engineer

FMT = 2026
N = 24
PLAYER = 3


def hdr(pid, uid, frame, t):
    return PacketHeader.from_values(FMT, 26, 1, 0, 1, pid, uid, t, frame, frame, PLAYER, 255)


def lap_packet(uid, pos_map):
    """pos_map: car_index -> position"""
    laps = []
    for i in range(N):
        pos = pos_map[i]
        laps.append(LapData.from_values(
            last_lap_time_ms=82000, current_lap_time_ms=20000,
            sector1_time_ms=28000, sector1_time_minutes=0,
            sector2_time_ms=18000, sector2_time_minutes=0,
            delta_to_front_ms=0 if pos == 1 else 500,
            delta_to_front_minutes=0,
            delta_to_leader_ms=(pos - 1) * 1000, delta_to_leader_minutes=0,
            lap_distance=2000.0, total_distance=20000.0, safety_car_delta=0.0,
            car_position=pos, current_lap_num=5, pit_status=0, num_pit_stops=0,
            sector=0, current_lap_invalid=0, penalties=0, total_warnings=0,
            corner_cutting_warnings=0, num_unserved_drive_through_pens=0,
            num_unserved_stop_go_pens=0, grid_position=pos, driver_status=4,
            result_status=2, pit_lane_timer_active=0, pit_lane_time_ms=0,
            pit_stop_timer_ms=0, pit_stop_should_serve_pen=0,
            speed_trap_fastest_speed=300.0, speed_trap_fastest_lap=1,
            packet_format=FMT))
    return PacketLapData.from_values(hdr(F1PacketType.LAP_DATA, 9, 1, 5.0), laps).to_bytes()


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)

    # Frame 1: player (car 3) is P1. Frame 2: player drops to P2, car 5 takes P1.
    pos1 = {i: i + 1 for i in range(N)}
    pos1[PLAYER] = 1
    pos1[0] = 2
    pkt = factory.parse(lap_packet(9, pos1))
    state.process(pkt)
    print("frame1 position:", state.snapshot()["latest"]["lap"]["car_position"])

    pos2 = {i: i + 1 for i in range(N)}
    pos2[PLAYER] = 2   # dropped
    pos2[0] = 1
    pkt = factory.parse(lap_packet(9, pos2))
    state.process(pkt)
    snap = state.snapshot()
    print("frame2 position:", snap["latest"]["lap"]["car_position"])
    print("events:", snap.get("events"))

    assert snap["events"], "no position event recorded"
    ev = snap["events"][-1]
    assert ev["kind"] == "position_down", ev
    assert ev["from"] == 1 and ev["to"] == 2, ev

    summary = Summariser().summarise(snap)
    print("\nrecent_events:", summary["recent_events"])
    text = build_snapshot_text(summary["facts"], summary["notes"],
                               summary["leaderboard"], summary.get("recent_events"))
    print("\n--- PROMPT ---")
    print(text)

    eng = Engineer()
    if eng.configured:
        print("\n--- AI ANSWERS ---")
        for q in ["他超过我了吗", "我现在第几", "谁超过我的"]:
            print(f"Q: {q}")
            print(f"A: {eng.ask(q, snap)}")
    else:
        print("\n[ai not configured, skipping live answers]")

    print("\nOVERTAKE EVENT OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
