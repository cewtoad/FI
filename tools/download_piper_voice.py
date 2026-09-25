"""Download a Piper voice model into piper_models/ (T3.5).

piper-tts ships a downloader (``python -m piper.download_voices``). This wrapper
fetches the Chinese voice used as the default and places both the .onnx model
and its .onnx.json config in ``piper_models/`` next to the app, so the full pack
stays self-contained/offline.

Usage:
    py -3.12 -m tools.download_piper_voice                 # default zh_CN voice
    py -3.12 -m tools.download_piper_voice zh_CN-huayan-medium
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_VOICE = "zh_CN-huayan-medium"


def main() -> int:
    p = argparse.ArgumentParser(description="Download a Piper voice model")
    p.add_argument("voice", nargs="?", default=DEFAULT_VOICE,
                   help=f"voice id (default {DEFAULT_VOICE})")
    args = p.parse_args()

    from paths import app_root
    dest = app_root() / "piper_models"
    dest.mkdir(parents=True, exist_ok=True)

    print(f"Downloading Piper voice '{args.voice}' -> {dest}")
    try:
        rc = subprocess.call(
            [sys.executable, "-m", "piper.download_voices", args.voice,
             "--data-dir", str(dest)])
    except FileNotFoundError:
        print("piper-tts is not installed. Run: pip install \"piper-tts[zh]\"")
        return 1
    if rc != 0:
        print(f"downloader exited with {rc}")
        return rc

    models = sorted(dest.glob("*.onnx"))
    if not models:
        print("no .onnx model found after download")
        return 1
    print(f"OK: {models[-1]}")
    print("Set in .env:  TTS_PROVIDER=piper")
    print(f"             TTS_PIPER_VOICE={models[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
