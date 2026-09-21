"""R4 test: official OVERTAKE (OVTK) events from the EVENT packet.

Covers:
  1. Player overtakes someone -> event recorded, text has both sides
     ("你超过了 <被超车手名>") using the participants name map.
  2. Player is overtaken -> "你被 <超车方车手名> 超过".
  3. Opponent vs opponent -> field event with both driver names.
  4. Dedup: a leaderboard position diff in the same direction right after an
     official OVTK is NOT double-recorded.
  5. Fallback: without any OVTK, a position diff still produces an event
     (the pre-existing behaviour, see also tests_overtake.py).

Run: py -3.12 tests_event_ovtk.py
"""

from __future__ import annotations

import struct
import sys

from lib.f1_types import (F1PacketType, LapData, LiveryColour, Nationality,
                          PacketHeader, PacketLapData,
                          PacketParticipantsData, ParticipantData, Platform,
                          TelemetrySetting)
from lib.f1_types.team_id import TeamID26
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState

FMT = 2026
N = 24
UID = 33
PLAYER = 3
NAMES = {0: "维斯塔潘", 1: "诺里斯", 3: "我", 5: "汉密尔顿", 7: "勒克莱尔"}


def hdr(pid, frame, t):
    return PacketHeader.from_values(FMT, 26, 1, 0, 1, pid, UID, t, frame, frame,
                                    PLAYER, 255)


def participants_packet():
    parts = []
    for i in range(N):
        parts.append(ParticipantData.from_values(
            header=hdr(F1PacketType.PARTICIPANTS, 1, 0.0),
            ai_controlled=(i != PLAYER),
            driver_id=255, network_id=0,
            team_id=list(TeamID26)[i % len(TeamID26)],
            my_team=False, race_number=i + 1,
            nationality=list(Nationality)[0],
            name=NAMES.get(i, f"车手{i}"),
            your_telemetry=TelemetrySetting.RESTRICTED,
            show_online_names=True, platform=Platform.NONE,
            liveries=[LiveryColour.from_values(0, 0, 0)] * 4))
    return PacketParticipantsData.from_values(
        hdr(F1PacketType.PARTICIPANTS, 1, 0.0), N, parts).to_bytes()


def ovtk_packet(frame, t, overtaker, overtaken):
    """Raw EVENT packet: header + 'OVTK' + Overtake payload (two vehicleIdx)."""
    return (hdr(F1PacketType.EVENT, frame, t).to_bytes() + b"OVTK"
            + struct.pack("<BB", overtaker, overtaken))


def lap_packet(frame, t, pos_map):
    laps = []
    for i in range(N):
        pos = pos_map[i]
        laps.append(LapData.from_values(
            last_lap_time_ms=82000, current_lap_time_ms=20000,
            sector1_time_ms=28000, sector1_time_minutes=0,
            sector2_time_ms=18000, sector2_time_minutes=0,
            delta_to_front_ms=0 if pos == 1 else 500, delta_to_front_minutes=0,
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
    return PacketLapData.from_values(
        hdr(F1PacketType.LAP_DATA, frame, t), laps).to_bytes()


def pos_map(player_pos, swapped=None):
    m = {i: i + 1 for i in range(N)}
    m[PLAYER] = player_pos
    if swapped:
        m.update(swapped)
    return m


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)

    def feed(raw):
        pkt = factory.parse(raw)
        assert pkt is not None, factory.last_failure_reason
        state.process(pkt)

    feed(participants_packet())

    # 1) player (car 3) overtakes car 5.
    feed(ovtk_packet(10, 10.0, overtaker=PLAYER, overtaken=5))
    ev = state.snapshot()["events"]
    print("player overtake:", ev)
    assert ev and ev[-1]["kind"] == "position_up", ev
    assert ev[-1]["text"] == "你超过了 汉密尔顿", ev[-1]
    assert ev[-1]["from"] == PLAYER and ev[-1]["to"] == 5, ev[-1]

    # 2) car 7 overtakes the player.
    feed(ovtk_packet(11, 11.0, overtaker=7, overtaken=PLAYER))
    ev = state.snapshot()["events"]
    print("player overtaken:", ev[-1])
    assert ev[-1]["kind"] == "position_down", ev[-1]
    assert ev[-1]["text"] == "你被 勒克莱尔 超过", ev[-1]

    # 3) opponent vs opponent.
    feed(ovtk_packet(12, 12.0, overtaker=0, overtaken=1))
    ev = state.snapshot()["events"]
    print("field overtake:", ev[-1])
    assert ev[-1]["kind"] == "field_overtake", ev[-1]
    assert ev[-1]["text"] == "维斯塔潘 超过了 诺里斯", ev[-1]

    # 4) dedup: official OVTK then a matching leaderboard diff -> one event only.
    before = len(state.snapshot()["events"])
    feed(lap_packet(20, 20.0, pos_map(1)))                 # baseline: player P1
    feed(ovtk_packet(21, 21.0, overtaker=5, overtaken=PLAYER))   # official: you were overtaken
    assert len(state.snapshot()["events"]) == before + 1
    feed(lap_packet(22, 22.0, pos_map(2)))                 # diff P1->P2, same direction
    events = state.snapshot()["events"]
    print("after dedup diff, last event:", events[-1])
    assert len(events) == before + 1, "position diff was double-recorded after OVTK"
    assert events[-1]["text"] == "你被 汉密尔顿 超过", events[-1]

    # 5) fallback: no OVTK, pure position diff still records an event.
    state2 = TelemetryState()
    holder = {"n": 0}

    def feed2(raw):
        pkt = factory.parse(raw)
        assert pkt is not None, factory.last_failure_reason
        state2.process(pkt)

    feed2(lap_packet(30, 30.0, pos_map(4)))
    feed2(lap_packet(31, 31.0, pos_map(3)))                # gained a place, no OVTK
    events2 = state2.snapshot()["events"]
    print("fallback diff event:", events2)
    assert events2 and events2[-1]["kind"] == "position_up", events2
    assert events2[-1]["from"] == 4 and events2[-1]["to"] == 3, events2[-1]

    print("\nOVERTAKE (OVTK) EVENT OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
