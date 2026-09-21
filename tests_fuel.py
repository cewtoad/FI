"""Verify the fuel-per-lap fix: 5 laps of decreasing fuel -> fuel recommender works.

Run: py -3.12 tests_fuel.py
"""

from __future__ import annotations

from fake_data import (PACKET_FORMAT, make_lap, make_status)
from lib.f1_types import F1PacketType
from lib.telemetry_manager import PacketParserFactory
from receiver import PACKETS_ALL
from state import TelemetryState
from summariser import Summariser

UID = 555
LAP_LEN = 5000.0
FUEL_START = 50.0


def main():
    state = TelemetryState()
    factory = PacketParserFactory(PACKETS_ALL, None)
    frame = 1
    t = 0.0
    total = 0.0
    fuel = FUEL_START
    # 5 laps; fuel burns 1.5 kg per lap, sampled continuously.
    for lap_no in range(1, 6):
        steps = 40
        for step in range(steps):
            dist = LAP_LEN * step / steps
            cur_ms = int(83000 * step / steps)
            fuel = FUEL_START - (lap_no - 1) * 1.5 - 1.5 * step / steps
            raw = make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, UID, frame, t,
                           last_lap_ms=(83000 if lap_no > 1 else 0), cur_lap_ms=cur_ms,
                           lap_distance=dist, total_distance=total + dist,
                           cur_lap_num=lap_no, position=1, sector=0)
            pkt = factory.parse(raw)
            assert pkt is not None, factory.last_failure_reason
            state.process(pkt)
            raw2 = make_status(PACKET_FORMAT, UID, frame, t,
                               fuel_kg=fuel, fuel_laps=3.0, tyre_age=lap_no)
            pkt2 = factory.parse(raw2)
            assert pkt2 is not None, factory.last_failure_reason
            state.process(pkt2)
            frame += 1
            t += 0.8
        total += LAP_LEN

    snap = state.snapshot()
    print("fuel:", snap["fuel"])
    print("packet_errors:", snap["packet_errors"])

    assert not snap["packet_errors"], snap["packet_errors"]
    fuelinfo = snap["fuel"]
    assert fuelinfo.get("data_sufficient"), f"fuel data insufficient: {fuelinfo}"
    rate = fuelinfo.get("curr_fuel_rate_kg_per_lap")
    assert rate is not None and abs(rate - 1.5) < 0.05, f"fuel rate wrong: {rate}"
    print(f"fuel rate = {rate:.3f} kg/lap (expected ~1.5)")

    summary = Summariser().summarise(snap)
    print("summary.fuel_surplus_laps:", summary["facts"].get("fuel_surplus_laps"))
    print("summary.fuel_rate:", summary["facts"].get("fuel_rate_kg_per_lap"))
    assert summary["facts"].get("fuel_rate_kg_per_lap") is not None
    print("\nFUEL FIX OK")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
