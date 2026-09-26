"""Review P14: real packets -> TelemetryState -> RaceModel -> RadioDirector.

The unit tests for race_model/radio build hand-made snapshots whose shape drifted
from the real TelemetryState snapshot (session fields at the top level, the
"NO SAFETY CAR" spelling). This test goes through the real state so that the
snapshot contract is exercised end to end.

The Session packet is injected through ``_on_session`` with the *real* lib enums
because ``PacketSessionData`` has no ``from_values`` (fake_data.make_session is
broken for the same reason).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace as NS

from fake_data import make_lap, make_status
from lib.f1_types import F1PacketType
from lib.f1_types.common import SafetyCarType, SessionType24
from lib.telemetry_manager import PacketParserFactory
from race_model import RaceModel
from radio_director import RadioDirector
from radio_rules import build_default_rules
from receiver import PACKETS_CONSUMED
from state import TelemetryState

UID = 77


def _race_state(sc=SafetyCarType.NO_SAFETY_CAR, laps=5):
    f = PacketParserFactory(PACKETS_CONSUMED, logging.getLogger("t"))
    st = TelemetryState()

    def feed(raw):
        pkt = f.parse(raw)
        assert pkt is not None, f.last_failure_reason
        st.process(pkt)

    feed(make_lap(2025, F1PacketType.LAP_DATA, UID, 0, 0.0, last_lap_ms=0, cur_lap_ms=0,
                  lap_distance=0.0, total_distance=0.0, cur_lap_num=1, position=3, sector=0))
    race = next(x for x in SessionType24 if x.isRaceTypeSession())
    st._on_session(NS(m_sessionType=race, m_trackId=0, m_totalLaps=20,
                      m_isSpectating=False, m_spectatorCarIndex=255, m_weather=None,
                      m_safetyCarStatus=sc, m_pitStopWindowIdealLap=3,
                      m_pitStopWindowLatestLap=6, m_pitStopRejoinPosition=5,
                      m_trackLength=5000, m_weatherForecastSamples=[], m_marshalZones=[]))
    st.mark_dirty()
    frame = 1
    for lap in range(1, laps + 1):
        feed(make_status(2025, UID, frame, frame * 0.1, fuel_kg=50 - lap * 1.5,
                         fuel_laps=5, tyre_age=lap)); frame += 1
        feed(make_lap(2025, F1PacketType.LAP_DATA, UID, frame, frame * 0.1,
                      last_lap_ms=85000 + lap * 100, cur_lap_ms=1000,
                      lap_distance=100.0, total_distance=100.0 * lap,
                      cur_lap_num=lap, position=3, sector=0)); frame += 1
    return st


def test_race_model_reads_real_snapshot_contract():
    st = _race_state()
    m = RaceModel()
    m.update(st.snapshot(), 0.0)
    d = m.latest_dict()
    assert d["session_kind"] == "race"
    assert d["pit_window"]["state"] == "open"          # ideal 3 <= lap 5 <= latest 6
    assert d["flags"]["safety_car_active"] is False    # "NO_SAFETY_CAR" is not a SC
    # +100ms/lap must survive (laps were wrongly dropped as SC laps before)
    assert abs(d["stint"]["pace_degradation_s_per_lap"] - 0.1) < 0.01
    json.dumps(d, allow_nan=False)                     # no Infinity in /api/state


def test_full_safety_car_is_detected():
    st = _race_state(sc=SafetyCarType.FULL_SAFETY_CAR)
    m = RaceModel()
    m.update(st.snapshot(), 0.0)
    assert m.latest_dict()["flags"]["safety_car_active"] is True


def test_director_fires_race_rule_from_real_state():
    st = _race_state()
    m = RaceModel()
    snap = st.snapshot()
    m.update(snap, 0.0)
    snap = {**snap, "race_model": m.latest_dict()}
    out = []
    d = RadioDirector(build_default_rules(), alert_sink=out.append)
    d.tick(snap, 100.0)
    ids = [a.id for a in out]
    assert "pit_window_open" in ids, ids
    assert "sc_deployed" not in ids, ids
