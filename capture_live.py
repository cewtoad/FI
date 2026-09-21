"""Continuous live capture - runs until manually stopped (Ctrl+C or killed).

Prints a live one-line status every 2s and dumps a full report on exit.
Designed to be started, left running while the user drives several laps, then
stopped.

Run: py -3.12 capture_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections import Counter

from lib.f1_types import F1PacketType
from receiver import PACKETS_ALL, TelemetryReceiver
from state import TelemetryState
from summariser import Summariser

PORT = 20777
STOP_FILE = "STOP_CAPTURE"


def _fmt(ms):
    if not ms or ms <= 0:
        return "-"
    m = ms // 60000
    r = ms % 60000
    return f"{m}:{r//1000:02d}.{r%1000:03d}" if m else f"{r/1000:.3f}"


async def main():
    from run import _setup_logging
    logger = _setup_logging(verbose=False)
    state = TelemetryState(error_logger=logger)
    receiver = TelemetryReceiver(state, port=PORT, bind_ip="127.0.0.1",
                                 interested=PACKETS_ALL, logger=logger)

    fmt_counter: Counter = Counter()
    type_counter: Counter = Counter()
    stop = asyncio.Event()

    def on_packet(pkt):
        fmt_counter[pkt.m_header.m_packetFormat] += 1
        type_counter[str(pkt.m_header.m_packetId)] += 1

    receiver.on_packet = on_packet

    print(f"[live] listening on 127.0.0.1:{PORT} - RUNNING UNTIL STOPPED")
    print(f"[live] create file '{STOP_FILE}' to stop gracefully")
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    recv_task = asyncio.create_task(receiver.run())

    summariser = Summariser()
    try:
        n = 0
        while not stop.is_set():
            await asyncio.sleep(2.0)
            n += 1
            snap = state.snapshot()
            lap = snap["latest"].get("lap", {})
            hist = snap["latest"].get("history", {})
            delta = snap.get("delta") or {}
            hist_laps = hist.get("laps", [])
            print(f"[{n*2:4d}s] lap={lap.get('current_lap_num')} "
                  f"pos={lap.get('car_position')} "
                  f"cur={_fmt(lap.get('current_lap_time_ms'))} "
                  f"last={_fmt(lap.get('last_lap_time_ms'))} "
                  f"delta={delta.get('delta_ms')} "
                  f"fuel={round(snap.get('fuel',{}).get('curr_fuel_rate_kg_per_lap'),3) if snap.get('fuel',{}).get('curr_fuel_rate_kg_per_lap') else '-'} "
                  f"fuelbuckets={sorted(getattr(state, '_fuel_at_lap_end', {}).keys())} "
                  f"fuelrec={sorted(getattr(state, '_fuel_recorded', set()))} "
                  f"hist_laps={len(hist_laps)} "
                  f"accepted={receiver.frames} "
                  f"gate_drops={receiver.dropped_gate} "
                  f"errors={snap.get('packet_errors')}", flush=True)
            if os.path.exists(STOP_FILE):
                print("[live] stop file detected, halting.")
                break
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    snap = state.snapshot()
    summary = summariser.summarise(snap)
    report = {
        "packet_formats_seen": dict(fmt_counter),
        "packet_types_seen": dict(type_counter),
        "receiver_stats": receiver.stats(),
        "packet_errors": snap.get("packet_errors"),
        "latest_lap": snap["latest"].get("lap"),
        "history": snap["latest"].get("history"),
        "history_laps": snap["latest"].get("history", {}).get("laps"),
        "fuel": snap.get("fuel"),
        "trends": snap.get("trends"),
        "summary": summary,
    }
    print("\n" + "=" * 60)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    with open("capture_live_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print("\n[live] wrote capture_live_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
