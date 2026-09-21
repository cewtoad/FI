"""End-to-end recorder check: web server + fake UDP -> session file has laps + qa."""

import asyncio
import json
import socket
import sys
import time
import urllib.request

from fake_data import PACKET_FORMAT, make_lap, make_status
from lib.f1_types import F1PacketType

PORT = 20810
WEB = 8777
HOST = "127.0.0.1"


def send_stream():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    uid = 4242
    frame = 1
    t = 0.0
    total = 0.0
    for lap_no in range(1, 4):
        for step in range(20):
            dist = 5000 * step / 20
            cur = int(83000 * step / 20)
            fuel = 50 - (lap_no - 1) * 1.5 - 1.5 * step / 20
            sock.sendto(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, uid, frame, t,
                                 (83000 if lap_no > 1 else 0), cur, dist, total + dist,
                                 lap_no, 1, 0), (HOST, PORT))
            sock.sendto(make_status(PACKET_FORMAT, uid, frame, t, fuel, 3.0, lap_no),
                        (HOST, PORT))
            frame += 1
            t += 0.8
            time.sleep(0.005)
        total += 5000
    sock.close()


async def main():
    import threading
    from webui import serve

    # Start web+udp server in a thread
    def run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        serve(port=PORT, web_port=WEB)

    th = threading.Thread(target=run_server, daemon=True)
    th.start()
    await asyncio.sleep(2.0)

    # Send fake telemetry (blocking ok, small)
    send_stream()
    await asyncio.sleep(0.5)

    # Hit /api/state a few times so laps get recorded
    for _ in range(4):
        try:
            urllib.request.urlopen(f"http://{HOST}:{WEB}/api/state", timeout=3).read()
        except Exception as e:
            print("state err", e)
        await asyncio.sleep(0.3)

    # One Q&A (needs AI; tolerate failure)
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{WEB}/api/ask",
            data=json.dumps({"question": "我圈速多少"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
        print("qa answer:", resp.get("answer"))
    except Exception as e:
        print("qa err (ok if no ai):", e)

    # Export endpoint
    data = urllib.request.urlopen(f"http://{HOST}:{WEB}/api/export", timeout=5).read()
    exported = json.loads(data.decode())
    print("export keys:", list(exported.keys()))
    print("laps recorded:", len(exported.get("laps", [])))
    print("qa recorded:", len(exported.get("qa", [])))

    from pathlib import Path
    files = sorted(Path("sessions").glob("session_*.json"))
    print("session files:", [f.name for f in files])
    if files:
        latest = json.loads(files[-1].read_text(encoding="utf-8"))
        print("latest file laps:", len(latest.get("laps", [])))

    print("RECORDER TEST DONE")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
