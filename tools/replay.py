"""Replay a ``.f1rec`` capture back through the receiver.

Two consumers:

  * ``replay_into(handle_raw, path, ...)`` - a pure function tests call with
    the receiver's ``_handle_raw`` (or any ``bytes -> None`` sink). Timing is
    injected via ``clock`` (default ``time.sleep``), so tests can pass a no-op
    clock for instant replay.
  * the CLI replays the file to a real UDP port (so a live app receives it) or
    with ``--in-process`` feeds a fresh receiver directly, no sockets involved.

Usage::

    py -3.12 -m tools.replay sessions/race.f1rec --port 20777
    py -3.12 -m tools.replay sessions/race.f1rec --speed 2 --port 20777
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from tools.udp_record import RECORD_HEADER, MAX_DATAGRAM


def read_records(path) -> list:
    """Return ``[(rel_ms, payload), ...]`` parsed from a ``.f1rec`` file."""
    out = []
    with open(path, "rb") as fh:
        buf = fh.read()
    off = 0
    n = len(buf)
    while off + RECORD_HEADER.size <= n:
        rel_ms, length = RECORD_HEADER.unpack_from(buf, off)
        off += RECORD_HEADER.size
        if length > MAX_DATAGRAM or off + length > n:
            break
        out.append((rel_ms, buf[off:off + length]))
        off += length
    return out


def replay_into(handle_raw: Callable[[bytes], None], path,
                speed: float = 1.0, clock: Callable[[float], None] = time.sleep,
                loop: bool = False) -> int:
    """Feed each recorded payload to ``handle_raw`` honouring the timing.

    ``clock`` receives the number of seconds to wait (default ``time.sleep``);
    pass a no-op to replay instantly. Returns the number of packets replayed.
    """
    records = read_records(path)
    if not records:
        return 0
    if speed <= 0:
        speed = float("inf")
    count = 0
    prev = records[0][0]
    while True:
        for rel_ms, payload in records:
            if rel_ms > prev:
                clock((rel_ms - prev) / 1000.0 / speed)
            prev = rel_ms
            handle_raw(payload)
            count += 1
        if not loop:
            break
    return count


def replay_to_udp(path, host: str = "127.0.0.1", port: int = 20777,
                  speed: float = 1.0, clock: Callable[[float], None] = time.sleep,
                  loop: bool = False) -> int:
    """Replay the capture to a UDP endpoint (as if the game sent it)."""
    import socket

    records = read_records(path)
    if not records:
        return 0
    if speed <= 0:
        speed = float("inf")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    count = 0
    prev = records[0][0]
    try:
        while True:
            for rel_ms, payload in records:
                if rel_ms > prev:
                    clock((rel_ms - prev) / 1000.0 / speed)
                prev = rel_ms
                sock.sendto(payload, (host, port))
                count += 1
            if not loop:
                break
    finally:
        sock.close()
    return count


def main() -> int:
    p = argparse.ArgumentParser(description="Replay a .f1rec F1 UDP capture")
    p.add_argument("path", help="input .f1rec file")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=20777)
    p.add_argument("--speed", type=float, default=1.0,
                   help="playback speed multiplier (0 = as fast as possible)")
    p.add_argument("--loop", action="store_true", help="replay forever")
    args = p.parse_args()

    print(f"Replaying {args.path} -> udp://{args.host}:{args.port} "
          f"(speed={args.speed})")
    try:
        n = replay_to_udp(args.path, args.host, args.port, args.speed,
                          loop=args.loop)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    print(f"done: {n} packets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
