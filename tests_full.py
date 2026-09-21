"""Full path test: real UDP -> receiver(gate) -> state -> summariser -> panel.

Runs the console UI render once (not looping) to verify it doesn't crash and
prints something sensible. Run: py -3.12 tests_full.py
"""

from __future__ import annotations

import asyncio
import io
import socket
import sys

from console_ui import ConsoleUI
from fake_data import (PACKET_FORMAT, make_lap, make_status, make_telemetry)
from lib.f1_types import F1PacketType
from receiver import TelemetryReceiver
from state import TelemetryState
from summariser import Summariser

PORT = 20783
HOST = "127.0.0.1"


async def main():
    state = TelemetryState()
    receiver = TelemetryReceiver(state, port=PORT, bind_ip=HOST)
    recv_task = asyncio.create_task(receiver.run())
    await asyncio.sleep(0.3)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    uid = 77
    frame = 1
    t = 0.0
    # Two laps worth, then a third lap start.
    for lap_no, (lap_ms) in ((1, 81000), (2, 80250)):
        for step in range(0, 60):
            dist = 5000 * step / 60
            cur = int(lap_ms * step / 60)
            sock.sendto(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, uid, frame, t,
                                 lap_ms if lap_no == 2 else 0, cur, dist,
                                 (lap_no - 1) * 5000 + dist, lap_no, 5, 0), (HOST, PORT))
            sock.sendto(make_telemetry(PACKET_FORMAT, uid, frame, t,
                                       295, 8, 1.0, 0.0, 100), (HOST, PORT))
            sock.sendto(make_status(PACKET_FORMAT, uid, frame, t,
                                    90.0 - lap_no, 4.0 - lap_no * 0.1, lap_no), (HOST, PORT))
            frame += 1
            t += 0.8
            await asyncio.sleep(0.002)
    # lap 3 start (time proportional to distance, same 5000m -> 80s rate)
    for step in range(0, 20):
        dist = 5000 * step / 200
        cur = int(80000 * step / 200)
        sock.sendto(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, uid, frame, t,
                             80250, cur, dist, 10000 + dist, 3, 4, 0), (HOST, PORT))
        sock.sendto(make_telemetry(PACKET_FORMAT, uid, frame, t,
                                   300, 8, 1.0, 0.0, 115), (HOST, PORT))
        frame += 1
        t += 0.8
        await asyncio.sleep(0.002)

    await asyncio.sleep(0.3)
    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass
    sock.close()

    snap = state.snapshot()
    summary = Summariser().summarise(snap)
    stats = receiver.stats()

    # Render once, capturing output, to make sure the UI works.
    ui = ConsoleUI(mode="timetrial")
    ui.render(summary, stats, connected=receiver.frames > 0)

    print("RECEIVER STATS:", stats)
    assert stats["accepted"] > 0
    assert stats["dropped_gate"] >= 0
    assert snap["trends"]["best_lap_ms"] == 80250, snap["trends"]
    assert summary["facts"]["tyre_temp_c"] == [115, 115, 115, 115]
    assert any("胎温过高" in n for n in summary["notes"]), summary["notes"]
    print("\nFULL PATH OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
