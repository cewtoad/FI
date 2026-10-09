"""Entry point for the standalone F1 TR receiver.

Usage:
    py -3.12 run.py --port 20777                 # console panel
    py -3.12 run.py --web                        # web UI (recommended)
    py -3.12 run.py --port 20777 --mode race
    py -3.12 run.py --port 20777 --json          # dump raw JSON instead of panel

Press Ctrl+C to stop.

Console and web both assemble through ``app.build_app`` so radio / race-model
/ ticker stay consistent. Console is still a slim panel (no LLM Q&A UI); it
just no longer bypasses the composition root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from app import build_app
from console_ui import ConsoleUI
from receiver import DEFAULT_PORT


def _setup_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("f1_tr")


async def _panel_loop(app, args: argparse.Namespace) -> None:
    ui = ConsoleUI(mode=args.mode)
    while True:
        await asyncio.sleep(args.interval)
        snap = app.state.snapshot()
        summary = app.summariser.summarise(snap)
        if args.json:
            print(json.dumps({"summary": summary, "stats": app.receiver.stats()},
                             indent=2, ensure_ascii=False, default=str), flush=True)
        else:
            ui.render(summary, app.receiver.stats(),
                      connected=app.receiver.frames > 0)


async def _main(args: argparse.Namespace) -> None:
    logger = _setup_logging(args.verbose)
    # Console shares the composition root with web/voice so radio + race model
    # are present. recording=False: the console panel is a live view, not a
    # session capture entry point (web/voice handle that).
    app = build_app(port=args.port, bind_ip=args.bind_ip, logger=logger,
                    recording=False, mode="console")
    app.start_background()
    logger.info("Mode=%s (console via build_app) radio=%s race_model=%s",
                args.mode, app.radio is not None, app.race_model is not None)
    try:
        tasks = [
            asyncio.create_task(app.receiver.run()),
            asyncio.create_task(_panel_loop(app, args)),
        ]
        await asyncio.gather(*tasks)
    finally:
        app.shutdown()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F1 telemetry receiver (TR layer)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port to bind")
    p.add_argument("--bind-ip", default="127.0.0.1",
                   help="IP the web panel binds to (console mode: UDP bind)")
    p.add_argument("--udp-bind", default=None,
                   help="IP the UDP receiver binds to (web mode; default 127.0.0.1, "
                        "use 0.0.0.0 for a console/host broadcasting to this PC)")
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
        # --mode / --interval / --json are console-panel options; warn instead
        # of silently ignoring them in web mode.
        if args.mode != "timetrial":
            print("warn: --mode has no effect in --web mode", file=sys.stderr)
        if args.interval != 1.0:
            print("warn: --interval has no effect in --web mode", file=sys.stderr)
        if args.json:
            print("warn: --json has no effect in --web mode", file=sys.stderr)
        serve(port=args.port, web_port=args.web_port, bind_ip=args.bind_ip,
              udp_bind=args.udp_bind, logger=logger)
        return
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
