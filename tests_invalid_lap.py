"""R2 regression test: invalid laps (cut / off-track) must not pollute the
best-lap reference or the pace trend, but must stay visible in lap history.

Scenario (fake lap-data stream):
    lap 1: 81.000s  valid
    lap 2: 78.500s  INVALID (faster than any valid lap - the trap)
    lap 3: 80.000s  valid
    lap 4: (first packet only - triggers lap 3 completion)

Expected: best_lap_ms == 80000 (lap 3), trend == [81000, 80000],
lap_records has all 3 laps with lap 2 marked invalid, delta best lap == 3.

Also covers the SessionHistory side: m_lapValidBitFlags 0x0A (sector bits only,
no 0x01 lap-valid bit) must read as invalid, and best_lap_time_lap_num pointing
at an invalid lap must be nulled.

Run: py -3.12 tests_invalid_lap.py
"""

from __future__ import annotations

import struct
import sys

from fake_data import PACKET_FORMAT, make_lap
from lib.f1_types import F1PacketType, PacketHeader
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState
from summariser import Summariser

UID = 2024
LAP_LEN = 5000.0

# LapHistoryData: lapTime(I) s1ms(H) s1min(B) s2ms(H) s2min(B) s3ms(H) s3min(B) flags(B)
LAP_REC = struct.Struct("<IHBHBHBB")


def feed_lap(factory, state, lap_no, last_lap_ms, invalid):
    """Drive one full lap of lap-data packets, then the first packet of the
    next lap so the boundary fires."""
    steps = 20
    lap_ms = {1: 81000, 2: 78500, 3: 80000}[lap_no]
    frame = 100 * lap_no
    for step in range(steps):
        dist = LAP_LEN * step / steps
        cur = int(lap_ms * step / steps)
        raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, UID, frame + step,
                       0.8 * (frame + step), last_lap_ms, cur, dist,
                       (lap_no - 1) * LAP_LEN + dist, lap_no, 1, 0,
                       current_lap_invalid=1 if invalid else 0)
        pkt = factory.parse(raw)
        assert pkt is not None, factory.last_failure_reason
        state.process(pkt)


def session_history_packet(best_lap_num):
    hdr = PacketHeader.from_values(
        packet_format=PACKET_FORMAT, game_year=25, game_major_version=1,
        game_minor_version=0, packet_version=1,
        packet_type=F1PacketType.SESSION_HISTORY, session_uid=UID,
        session_time=600.0, frame_identifier=900, overall_frame_identifier=900,
        player_car_index=0, secondary_player_car_index=255)
    body = struct.pack("<BBBBBBB", 0, 3, 0, best_lap_num, 0, 0, 0)
    body += LAP_REC.pack(81000, 27000, 0, 27000, 0, 27000, 0, 0x01)   # lap 1 valid
    body += LAP_REC.pack(78500, 26000, 0, 26000, 0, 26500, 0, 0x0A)   # lap 2: sector bits ONLY
    body += LAP_REC.pack(80000, 26500, 0, 26500, 0, 27000, 0, 0x01)   # lap 3 valid
    return hdr.to_bytes() + body


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)

    feed_lap(factory, state, 1, 0, invalid=False)
    feed_lap(factory, state, 2, 81000, invalid=True)    # invalid 78.5s - the trap
    feed_lap(factory, state, 3, 78500, invalid=False)
    # first packet of lap 4 completes lap 3
    raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, UID, 999, 800.0,
                   80000, 0, 0.0, 3 * LAP_LEN, 4, 1, 0)
    state.process(factory.parse(raw))

    snap = state.snapshot()
    trends = snap["trends"]
    print("lap_times_ms:", trends["lap_times_ms"])
    print("best_lap_ms:", trends["best_lap_ms"])
    print("lap_records:", trends["lap_records"])
    print("delta best_lap_num:", (snap.get("delta") or {}).get("best_lap_num"))

    assert trends["lap_times_ms"] == [81000, 80000], trends["lap_times_ms"]
    assert trends["best_lap_ms"] == 80000, trends["best_lap_ms"]
    assert (snap.get("delta") or {}).get("best_lap_num") == 3, snap.get("delta")
    records = trends["lap_records"]
    assert [r["lap_num"] for r in records] == [1, 2, 3], records
    assert [r["valid"] for r in records] == [True, False, True], records
    assert records[1]["lap_time_ms"] == 78500, records

    summary = Summariser().summarise(snap)
    print("lap_history:", summary["lap_history"])
    assert len(summary["lap_history"]) == 3, summary["lap_history"]
    assert summary["lap_history"][1].endswith("(无效)"), summary["lap_history"]
    assert "(无效)" not in summary["lap_history"][0], summary["lap_history"]

    # ---- SessionHistory: sector-only bits are NOT lap validity ----
    pkt = factory.parse(session_history_packet(best_lap_num=2))
    assert pkt is not None, factory.last_failure_reason
    state.process(pkt)
    hist = state.snapshot()["latest"]["history"]
    print("history laps:", [(l["lap_num"], l["valid"]) for l in hist["laps"]])
    print("best_lap_time_lap_num:", hist["best_lap_time_lap_num"])
    assert [l["valid"] for l in hist["laps"]] == [True, False, True], hist["laps"]
    assert hist["best_lap_time_lap_num"] is None, hist["best_lap_time_lap_num"]

    # And when the pointer targets a valid lap it must be kept.
    state2 = TelemetryState()
    pkt = factory.parse(session_history_packet(best_lap_num=3))
    state2.process(pkt)
    hist2 = state2.snapshot()["latest"]["history"]
    assert hist2["best_lap_time_lap_num"] == 3, hist2["best_lap_time_lap_num"]

    print("\nINVALID LAP HANDLING OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
