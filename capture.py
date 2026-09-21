"""Live capture with auto-stop, for verifying real game data.

Behaviour:
    - Bind UDP and watch for any packet (raw => we know the game is talking).
    - Detect "session start" via sessionUID + session-type appearance.
    - Once session detected: capture for RUN_SECONDS, then stop and report.
    - If no session within WAIT_SECONDS: stop and report what (if anything) arrived.

Run: py -3.12 capture.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import Counter

from lib.f1_types import F1PacketType
from receiver import PACKETS_ALL, TelemetryReceiver
from state import TelemetryState
from summariser import Summariser

PORT = 20777
WAIT_SECONDS = 10      # no-session timeout
RUN_SECONDS = 40       # capture length after session detected


async def main():
    from run import _setup_logging  # reuse logging
    logger = _setup_logging(verbose=False)

    state = TelemetryState(error_logger=logger)
    receiver = TelemetryReceiver(state, port=PORT, bind_ip="127.0.0.1",
                                 interested=PACKETS_ALL, logger=logger)

    start = asyncio.get_event_loop().time()
    session_t0 = None
    raw_seen = 0
    fmt_counter: Counter = Counter()
    type_counter: Counter = Counter()
    meaningful_signal = {"lap_data_with_lap": False, "session_pkt": False}

    def on_packet(pkt):
        nonlocal raw_seen
        raw_seen += 1
        h = pkt.m_header
        fmt_counter[h.m_packetFormat] += 1
        type_counter[str(h.m_packetId)] += 1
        # Meaningful "in a session" signals: a Session packet, or a LapData
        # packet whose player lap number is > 0 (a real lap in progress).
        if h.m_packetId == F1PacketType.SESSION:
            meaningful_signal["session_pkt"] = True
        elif h.m_packetId == F1PacketType.LAP_DATA:
            try:
                lap = pkt.m_lapData[h.m_playerCarIndex]
                if lap.m_currentLapNum > 0:
                    meaningful_signal["lap_data_with_lap"] = True
            except Exception:
                pass

    receiver.on_packet = on_packet

    print(f"[capture] listening on 127.0.0.1:{PORT}")
    print(f"[capture] waiting up to {WAIT_SECONDS}s for a session, then capturing {RUN_SECONDS}s")
    recv_task = asyncio.create_task(receiver.run())

    try:
        while True:
            await asyncio.sleep(0.2)
            now = asyncio.get_event_loop().time()
            detected = meaningful_signal["session_pkt"] or meaningful_signal["lap_data_with_lap"]
            if detected and session_t0 is None:
                session_t0 = now
                why = "lap in progress" if meaningful_signal["lap_data_with_lap"] else "session packet"
                print(f"[capture] SESSION DETECTED ({why}, uid={state.session_uid}) "
                      f"at +{now-start:.1f}s -> capturing {RUN_SECONDS}s")
            if session_t0 is not None and (now - session_t0) >= RUN_SECONDS:
                print(f"[capture] captured {RUN_SECONDS}s of session, stopping.")
                break
            if session_t0 is None and (now - start) >= WAIT_SECONDS:
                print(f"[capture] no session within {WAIT_SECONDS}s, stopping.")
                break
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    snap = state.snapshot()
    summary = Summariser().summarise(snap)

    report = {
        "raw_packets_accepted": raw_seen,
        "session_started": state.session_started,
        "session_uid": state.session_uid,
        "packet_formats_seen": dict(fmt_counter),
        "packet_types_seen": dict(type_counter),
        "receiver_stats": receiver.stats(),
        "packet_errors": snap.get("packet_errors"),
        "latest": snap.get("latest"),
        "summary": summary,
    }
    print("\n" + "=" * 60)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    with open("capture_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print("\n[capture] wrote capture_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
