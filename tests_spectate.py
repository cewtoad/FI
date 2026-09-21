"""Spectator test: isSpectating=True, focus car should follow spectatorCarIndex."""

from __future__ import annotations

from lib.f1_types import F1PacketType, PacketHeader, PacketLapData, LapData
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState

FMT = 2026
N = 24
PLAYER_HDR = 1     # header says player car 1
SPECTATE_CAR = 0   # but we're watching car 0


def hdr(pid, uid, frame, t):
    return PacketHeader.from_values(FMT, 26, 1, 0, 1, pid, uid, t, frame, frame,
                                    PLAYER_HDR, 255)


def lap_packet(uid):
    laps = []
    for i in range(N):
        laps.append(LapData.from_values(
            last_lap_time_ms=80000 + i, current_lap_time_ms=20000,
            sector1_time_ms=28000, sector1_time_minutes=0,
            sector2_time_ms=18000, sector2_time_minutes=0,
            delta_to_front_ms=500, delta_to_front_minutes=0,
            delta_to_leader_ms=(i) * 1000, delta_to_leader_minutes=0,
            lap_distance=2000.0, total_distance=20000.0, safety_car_delta=0.0,
            car_position=i + 1, current_lap_num=5, pit_status=0, num_pit_stops=0,
            sector=0, current_lap_invalid=0, penalties=0, total_warnings=0,
            corner_cutting_warnings=0, num_unserved_drive_through_pens=0,
            num_unserved_stop_go_pens=0, grid_position=i + 1, driver_status=4,
            result_status=2, pit_lane_timer_active=0, pit_lane_time_ms=0,
            pit_stop_timer_ms=0, pit_stop_should_serve_pen=0,
            speed_trap_fastest_speed=300.0, speed_trap_fastest_lap=1,
            packet_format=FMT))
    return PacketLapData.from_values(hdr(F1PacketType.LAP_DATA, uid, 1, 5.0), laps).to_bytes()


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)

    # No session packet yet: header player index should be used (car 1 -> P2).
    pkt = factory.parse(lap_packet(1))
    state.process(pkt)
    assert state.player_car_index == PLAYER_HDR, state.player_car_index
    assert state.snapshot()["latest"]["lap"]["car_position"] == 2
    print("before session: focus on header car", PLAYER_HDR, "-> P2")

    # Simulate a session packet announcing spectate on car 0.
    state.is_spectating = True
    state.spectator_car_index = SPECTATE_CAR
    pkt = factory.parse(lap_packet(1))
    state.process(pkt)
    assert state.player_car_index == SPECTATE_CAR, state.player_car_index
    pos = state.snapshot()["latest"]["lap"]["car_position"]
    print("after spectate:  focus on spectator car", SPECTATE_CAR, "-> P", pos)
    assert pos == 1, pos

    print("\nSPECTATOR FOCUS OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
