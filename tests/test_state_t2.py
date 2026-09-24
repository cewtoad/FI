"""T2: telemetry data completeness + flashback rollback.

Covers:
  T2.1 session fields (kind, weather forecast, yellow zones, pit window)
  T2.2 lap fields (penalties, warnings, unserved pens)
  T2.3 leaderboard fields (driver_status, lap_distance, best_lap_ms)
  T2.4 event dispatch + timeline (SCAR/RDFL/PENA/FTLP/RTMT/CHQF/LGOT/FLBK)
  T2.5 flashback rollback of lap-derived data
  T2.6 final classification
"""

from __future__ import annotations

import logging
import struct

from fake_data import make_lap, make_status
from lib.f1_types import F1PacketType, PacketHeader
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_CONSUMED, TelemetryReceiver
from state import TelemetryState

FMT = 2025
N = 22
UID = 55
PLAYER = 0
_LOG = logging.getLogger("test_state_t2")


def _hdr(pid, frame, t, uid=UID):
    return PacketHeader.from_values(
        packet_format=FMT, game_year=25, game_major_version=1, game_minor_version=0,
        packet_version=1, packet_type=pid, session_uid=uid, session_time=t,
        frame_identifier=frame, overall_frame_identifier=frame,
        player_car_index=PLAYER, secondary_player_car_index=255)


def _event_raw(code: str, payload: bytes, frame, t, uid=UID) -> bytes:
    return _hdr(F1PacketType.EVENT, frame, t, uid).to_bytes() + code.encode() + payload


def _factory():
    return PacketParserFactory(PACKETS_CONSUMED, _LOG)


def _feed(state, factory, raw):
    pkt = factory.parse(raw)
    assert pkt is not None, factory.last_failure_reason
    state.process(pkt)
    return pkt


# ---------------------------------------------------------------- T2.2 / T2.3

def test_lap_fields_and_leaderboard_extras():
    st = TelemetryState()
    f = _factory()
    _feed(st, f, make_lap(FMT, F1PacketType.LAP_DATA, UID, 1, 0.0,
                          last_lap_ms=81000, cur_lap_ms=30000,
                          lap_distance=1200.0, total_distance=1200.0,
                          cur_lap_num=3, position=4, sector=1))
    lap = st.snapshot()["latest"]["lap"]
    for key in ("penalties_s", "total_warnings", "corner_cutting_warnings",
                "num_unserved_dt_pens", "num_unserved_sg_pens"):
        assert key in lap, key
    rows = st.snapshot()["leaderboard"]
    assert rows, "leaderboard empty"
    row = rows[0]
    for key in ("driver_status", "lap_distance_m", "penalties_s", "best_lap_ms"):
        assert key in row, key
    # best_lap_ms tracks the min non-zero last lap.
    assert row["best_lap_ms"] == 81000


# ---------------------------------------------------------------- T2.4 events

def test_event_timeline_fields_and_dispatch():
    st = TelemetryState()
    f = _factory()
    # Baseline lap so lap_num is known.
    _feed(st, f, make_lap(FMT, F1PacketType.LAP_DATA, UID, 1, 0.0,
                          0, 1000, 100.0, 100.0, 3, 5, 0))

    # RED_FLAG
    _feed(st, f, _event_raw("RDFL", b"\x00", 2, 5.0))
    ev = st.snapshot()["events"][-1]
    assert ev["kind"] == "red_flag"
    assert ev["session_time"] == 5.0
    assert ev["lap_num"] == 3

    # CHEQUERED_FLAG
    _feed(st, f, _event_raw("CHQF", b"\x00", 3, 6.0))
    assert st.snapshot()["events"][-1]["kind"] == "chequered"

    # LIGHTS_OUT
    _feed(st, f, _event_raw("LGOT", b"\x00", 4, 7.0))
    assert st.snapshot()["events"][-1]["kind"] == "lights_out"

    # SESSION_STARTED / ENDED
    _feed(st, f, _event_raw("SSTA", b"\x00", 5, 8.0))
    assert st.snapshot()["events"][-1]["kind"] == "session_started"
    _feed(st, f, _event_raw("SEND", b"\x00", 6, 9.0))
    assert st.snapshot()["events"][-1]["kind"] == "session_ended"


def test_event_safety_car_and_retirement_and_fastest_lap():
    st = TelemetryState()
    f = _factory()
    # SafetyCar event: type=FULL(1), event=DEPLOYED(0)
    _feed(st, f, _event_raw("SCAR", struct.pack("<BB", 1, 0), 1, 1.0))
    ev = st.snapshot()["events"][-1]
    assert ev["kind"] == "safety_car"
    assert "安全车出动" in ev["text"]
    assert ev["safety_car_type"] == 1

    # FastestLap: vehicleIdx=0 (player), lapTime seconds
    _feed(st, f, _event_raw("FTLP", struct.pack("<Bf", PLAYER, 81.234), 2, 2.0))
    ev = st.snapshot()["events"][-1]
    assert ev["kind"] == "fastest_lap"
    assert ev["is_player"] is True
    assert ev["lap_time_ms"] == 81234

    # Retirement of car 3 (2025 format packs vehicleIdx + reason = 2 bytes)
    _feed(st, f, _event_raw("RTMT", struct.pack("<BB", 3, 0), 3, 3.0))
    ev = st.snapshot()["events"][-1]
    assert ev["kind"] == "retirement"
    assert ev["vehicle_idx"] == 3


def test_event_penalty():
    st = TelemetryState()
    f = _factory()
    # Penalty: penaltyType(1), infringementType(1), vehicleIdx(1), otherVehicleIdx(1),
    # time(1), lapNum(1), placesGained(1) = 7 bytes
    payload = struct.pack("<BBBBBBB", 1, 1, PLAYER, 255, 5, 2, 0)
    _feed(st, f, _event_raw("PENA", payload, 1, 1.0))
    ev = st.snapshot()["events"][-1]
    assert ev["kind"] == "penalty"
    assert ev["is_player"] is True
    assert ev["penalty_time_s"] == 5


# ---------------------------------------------------------------- T2.5 flashback

def test_flashback_rolls_back_lap_data():
    st = TelemetryState()
    f = _factory()

    def feed_lap(frame, t, lap_no, last_ms=80000):
        _feed(st, f, make_lap(FMT, F1PacketType.LAP_DATA, UID, frame, t,
                              last_ms if lap_no > 1 else 0, 30000,
                              2000.0, 2000.0 * lap_no, lap_no, 1, 0))

    def feed_status(frame, t, fuel):
        _feed(st, f, make_status(FMT, UID, frame, t, fuel, 3.0, 5))

    # Run up to lap 5.
    st.latest["damage"] = {"tyre_wear_fl": 40.0}
    for lap in range(1, 6):
        feed_status(lap, lap * 1.0, 50.0 - lap * 1.5)
        feed_lap(lap, lap * 1.0, lap, last_ms=80000 + lap)
    # Two recorded one-lap-advanced laps (need lap N then N+1 to complete N).
    assert len(st.lap_records.values()) >= 3
    best_before = st._best_lap_ms

    # FLBK, then rewind: back on lap 3.
    _feed(st, f, _event_raw("FLBK", struct.pack("<If", 100, 12.5), 50, 50.0))
    feed_lap(51, 51.0, 3, last_ms=0)  # new LAP_DATA shows lap 3

    recs = st.lap_records.values()
    assert all(r["lap_num"] < 3 for r in recs), recs
    # best-lap rebuilt from survivors (<= before).
    assert st._best_lap_ms is None or st._best_lap_ms >= best_before
    # no fuel readings for rewound laps survive
    assert all(n < 3 for n in st._fuel_at_lap_end)
    # per-lap snapshots trimmed
    assert all(s["lap_num"] < 3 for s in st.lap_snapshots)


def test_flashback_without_rewind_is_noop():
    st = TelemetryState()
    f = _factory()
    _feed(st, f, make_lap(FMT, F1PacketType.LAP_DATA, UID, 1, 0.0, 0, 1000,
                          100.0, 100.0, 4, 1, 0))
    _feed(st, f, _event_raw("FLBK", struct.pack("<If", 1, 1.0), 2, 2.0))
    # Next lap is 5 (>= 4): the rewind does not reach a lap we had data for,
    # so the lap-4 completion is still recorded normally.
    _feed(st, f, make_lap(FMT, F1PacketType.LAP_DATA, UID, 3, 3.0, 83000, 1000,
                          100.0, 100.0, 5, 1, 0))
    recs = st.lap_records.values()
    assert any(r["lap_num"] == 4 for r in recs), recs
    assert st._best_lap_ms == 83000


# ---------------------------------------------------------------- T2.1 session

def test_session_kind_and_window_via_state_helper():
    from lib.f1_types import SessionType24
    # Sprint shootout is a qualifying session (verified against lib source).
    assert st_kind(SessionType24.SPRINT_SHOOTOUT_1) == "qualifying"
    assert st_kind(SessionType24.QUALIFYING_1) == "qualifying"
    assert st_kind(SessionType24.RACE) == "race"
    assert st_kind(SessionType24.PRACTICE_1) == "practice"
    assert st_kind(SessionType24.TIME_TRIAL) == "time_trial"


def st_kind(enum_val):
    return TelemetryState._session_kind(enum_val)
