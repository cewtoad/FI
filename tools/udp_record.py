"""Raw UDP capture to a replayable ``.f1rec`` file.

The existing capture.py only writes a *summary* (capture_report.json), so there
was no byte-level corpus to replay against. This tool records the exact UDP
payloads the game sent, with a relative timestamp per datagram, so tests and
offline debugging can feed them back through the real receiver/parsers.

File format (little-endian), one record per datagram::

    [4B relative milliseconds since first packet][4B payload length][payload]

``relative milliseconds`` is a uint32. The first packet's timestamp is 0.

Usage::

    py -3.12 -m tools.udp_record --port 20777 --out sessions/race.f1rec
    py -3.12 -m tools.udp_record --port 20777 --out x.f1rec --max-seconds 120
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Optional

RECORD_HEADER = struct.Struct("<II")  # (rel_ms, length)
MAX_DATAGRAM = 16384


def write_record(fh, rel_ms: int, payload: bytes) -> None:
    """Append one datagram record to an open binary file handle."""
    fh.write(RECORD_HEADER.pack(int(rel_ms) & 0xFFFFFFFF, len(payload)))
    fh.write(payload)


def record_udp(port: int, out_path, duration: Optional[float] = None,
               bind_ip: str = "0.0.0.0", max_packets: Optional[int] = None,
               logger=None) -> int:
    """Listen on ``port`` and write datagrams to ``out_path``.

    Returns the number of packets written. Stops after ``duration`` seconds
    (if given) or ``max_packets`` packets (if given); otherwise runs until
    interrupted.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    sock.settimeout(1.0)
    start = time.monotonic()
    count = 0
    with open(out_path, "wb") as fh:
        while True:
            if duration is not None and (time.monotonic() - start) >= duration:
                break
            try:
                data, _addr = sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            rel_ms = int((time.monotonic() - start) * 1000)
            write_record(fh, rel_ms, data)
            count += 1
            if max_packets is not None and count >= max_packets:
                break
    sock.close()
    if logger is not None:
        logger.info("recorded %d packets to %s", count, out_path)
    return count


def main() -> int:
    p = argparse.ArgumentParser(description="Record F1 UDP telemetry to .f1rec")
    p.add_argument("--port", type=int, default=20777)
    p.add_argument("--bind-ip", default="0.0.0.0")
    p.add_argument("--out", required=True, help="output .f1rec path")
    p.add_argument("--max-seconds", type=float, default=None,
                   help="stop after N seconds")
    p.add_argument("--max-packets", type=int, default=None,
                   help="stop after N packets")
    args = p.parse_args()

    print(f"Recording UDP :{args.port} -> {args.out} (Ctrl+C to stop)")
    try:
        n = record_udp(args.port, args.out, duration=args.max_seconds,
                       bind_ip=args.bind_ip, max_packets=args.max_packets)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    print(f"done: {n} packets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
