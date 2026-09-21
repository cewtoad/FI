"""R1 regression test: a packet that makes factory.parse() RAISE must not kill
the receive loop.

Before the fix, a packetFormat=2022 (or older) packet raised
UnsupportedPacketFormat out of PacketParserFactory.parse(), which
TelemetryReceiver._handle_raw() did not catch - the asyncio receive task died
(console: whole program crashed; web: telemetry frozen forever).

Run: py -3.12 tests_parse_guard.py
"""

from __future__ import annotations

import asyncio
import socket
import sys

from fake_data import PACKET_FORMAT, make_lap
from lib.f1_types import F1PacketType, PacketHeader
from receiver import TelemetryReceiver
from state import TelemetryState

HOST = "127.0.0.1"


def old_format_packet() -> bytes:
    """A well-formed header with packetFormat=2022 (< MIN_PACKET_FORMAT) plus junk."""
    hdr = PacketHeader.from_values(
        packet_format=2022, game_year=22, game_major_version=1,
        game_minor_version=0, packet_version=1, packet_type=F1PacketType.SESSION,
        session_uid=777, session_time=0.0, frame_identifier=1,
        overall_frame_identifier=1, player_car_index=0,
        secondary_player_car_index=255)
    return hdr.to_bytes() + b"\x00" * 100


async def main():
    state = TelemetryState()
    # port=0 -> bind an ephemeral port so the test never fights a real receiver
    receiver = TelemetryReceiver(state, port=0, bind_ip=HOST)
    recv_task = asyncio.create_task(receiver.run())
    await asyncio.sleep(0.2)  # let the socket bind
    port = receiver.transport.m_socket.getsockname()[1]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # 1) The killer packet: must be dropped, never crash the loop.
    sock.sendto(old_format_packet(), (HOST, port))
    await asyncio.sleep(0.2)
    assert not recv_task.done(), "receive loop died on an old-format packet!"
    stats = receiver.stats()
    print("after bad packet:", stats)
    assert receiver.frames == 0, f"frames should be 0, got {receiver.frames}"
    assert stats["dropped_unparsed"] == 1, stats
    assert "parse-exception: UnsupportedPacketFormat" in stats["drop_reasons"], stats

    # 2) The loop must still be alive and accept good packets afterwards.
    sock.sendto(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, 42, 1, 0.0,
                         0, 1000, 100.0, 100.0, 1, 1, 0), (HOST, port))
    await asyncio.sleep(0.2)
    assert not recv_task.done(), "receive loop died after the bad packet!"
    stats = receiver.stats()
    print("after good packet:", stats)
    assert receiver.frames == 1, f"good packet not accepted: {stats}"
    assert stats["dropped_unparsed"] == 1, stats

    sock.close()
    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass

    print("\nPARSE GUARD OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
