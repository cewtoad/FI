"""Test the real UDP path: bind receiver, send fake packets, check state.

Run: py -3.12 tests_udp.py
"""

from __future__ import annotations

import asyncio
import socket

from fake_data import (PACKET_FORMAT, make_lap, make_status, make_telemetry)
from lib.f1_types import F1PacketType
from receiver import TelemetryReceiver
from state import TelemetryState

PORT = 20779
HOST = "127.0.0.1"


async def main():
    state = TelemetryState()
    receiver = TelemetryReceiver(state, port=PORT, bind_ip=HOST)

    recv_task = asyncio.create_task(receiver.run())
    await asyncio.sleep(0.3)  # let the socket bind

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    uid = 42
    frame = 1
    t = 0.0
    for step in range(0, 50):
        dist = 5000 * step / 50
        cur = int(80000 * step / 50)
        sock.sendto(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, uid, frame, t,
                             0, cur, dist, dist, 1, 5, 0), (HOST, PORT))
        sock.sendto(make_telemetry(PACKET_FORMAT, uid, frame, t,
                                   300, 8, 1.0, 0.0, 98), (HOST, PORT))
        sock.sendto(make_status(PACKET_FORMAT, uid, frame, t,
                                100.0, 4.0, 1), (HOST, PORT))
        frame += 1
        t += 0.8
        await asyncio.sleep(0.005)

    await asyncio.sleep(0.3)
    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass
    sock.close()

    snap = state.snapshot()
    print("frames received:", receiver.frames)
    print("packet counts:", snap["packet_counts"])
    print("latest lap:", snap["latest"].get("lap"))
    print("latest car speed:", snap["latest"].get("car", {}).get("speed_kph"))
    assert receiver.frames >= 150, f"expected >=150 packets, got {receiver.frames}"
    assert snap["packet_counts"].get("CAR_TELEMETRY", 0) == 50
    assert snap["latest"]["car"]["speed_kph"] == 300
    print("\nUDP PATH OK")


if __name__ == "__main__":
    asyncio.run(main())
