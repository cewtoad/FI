"""Entry point for the standalone F1 TR receiver.

Usage:
    py -3.12 run.py --port 20777                 # console panel
    py -3.12 run.py --web                        # web UI (recommended)
    py -3.12 run.py --port 20777 --mode race
    py -3.12 run.py --port 20777 --json          # dump raw JSON instead of panel

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from console_ui import ConsoleUI
from receiver import (DEFAULT_PORT, PACKETS_RACE, PACKETS_TIME_TRIAL,
                      TelemetryReceiver)
from state import TelemetryState
from summariser import Summariser


def _setup_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("f1_tr")


async def _panel_loop(state: TelemetryState, receiver: TelemetryReceiver,
                      args: argparse.Namespace) -> None:
    summariser = Summariser()
    ui = ConsoleUI(mode=args.mode)
    while True:
        await asyncio.sleep(args.interval)
        snap = state.snapshot()
        summary = summariser.summarise(snap)
        if args.json:
            print(json.dumps({"summary": summary, "stats": receiver.stats()},
                             indent=2, ensure_ascii=False, default=str), flush=True)
        else:
            ui.render(summary, receiver.stats(), connected=receiver.frames > 0)


async def _main(args: argparse.Namespace) -> None:
    logger = _setup_logging(args.verbose)
    state = TelemetryState(error_logger=logger)
    interested = PACKETS_RACE if args.mode == "race" else PACKETS_TIME_TRIAL
    receiver = TelemetryReceiver(
        state,
        port=args.port,
        bind_ip=args.bind_ip,
        interested=interested,
        logger=logger,
    )
    logger.info("Mode=%s  packets=%s", args.mode, sorted(str(p) for p in interested))
    tasks = [asyncio.create_task(receiver.run())]
    tasks.append(asyncio.create_task(_panel_loop(state, receiver, args)))
    await asyncio.gather(*tasks)


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F1 telemetry receiver (TR layer)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port to bind")
    p.add_argument("--bind-ip", default="127.0.0.1", help="IP to bind")
    p.add_argument("--mode", choices=["timetrial", "race"], default="timetrial",
                   help="Which packet set to subscribe to")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Seconds between UI refreshes")
    p.add_argument("--json", action="store_true",
                   help="Print raw JSON summaries instead of the text panel")
    p.add_argument("--web", action="store_true",
                   help="Serve the web UI instead of the console panel")
    p.add_argument("--web-port", type=int, default=8765,
                   help="Port for the web UI")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.web:
        from webui import serve
        logger = _setup_logging(args.verbose)
        serve(port=args.port, web_port=args.web_port, logger=logger)
        return
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
