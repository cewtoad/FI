"""Single build manifest shared by PyInstaller and the full-pack copy (T10a).

Both ``build_release.ps1`` and ``.github/workflows/release.yml`` used to repeat
the PyInstaller argument list, which drifted (they both still excluded
sounddevice/numpy). This module is the one place that knows:

  * RUNTIME_MODULES - top-level modules shipped in every pack (drives both the
    ``--hidden-import`` list and the full-pack file copy whitelist);
  * RESOURCE_DIRS   - read-only data directories (``--add-data`` / copy);
  * PROVIDER modules reached only by string import (PyInstaller cannot see
    them, so they must be hidden-imported).

Usage:
    py -3.12 build_manifest.py list            # print module names
    py -3.12 build_manifest.py hidden-imports  # one --hidden-import arg per line
    py -3.12 build_manifest.py full-copy --dest DIR   # copy the whitelist
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Modules the running app imports (directly or lazily). Used for both the
# full-pack copy whitelist and PyInstaller hidden imports.
RUNTIME_MODULES = [
    "FI.py",
    "run.py",
    "app.py",
    "state.py",
    "receiver.py",
    "summariser.py",
    "contracts.py",
    "config.py",
    "config_schema.py",
    "config_ui.py",
    "ticker.py",
    "speech.py",
    "radio_fx.py",
    "voices.py",
    "race_model.py",
    "radio_director.py",
    "radio_rules.py",
    "radio_templates.py",
    "names.py",
    "input_sources.py",
    "tts_text.py",
    "ptt_controller.py",
    "debrief.py",
    "profiles.py",
    "prompts.py",
    "llm_client.py",
    "engineer.py",
    "recorder.py",
    "report_txt.py",
    "webui.py",
    "console_ui.py",
    "voice.py",
    "voice_main.py",
    "voice_stt.py",
    "voice_tts.py",
    "voice_trigger.py",
    "stt_client.py",
    "tts_client.py",
    "audio.py",
    "paths.py",
    "ai_client.py",
    # Runtime tool used by FI.py --selftest (must ship in both packs).
    "tool_selftest.py",
]

# Modules imported by string (provider registries) - must be hidden-imported so
# PyInstaller's static analysis does not drop them.
HIDDEN_IMPORT_MODULES = [
    "tts_client",
    "voices",
    "stt_client",
    "radio_director",
    "radio_rules",
    "radio_templates",
    "race_model",
    "input_sources",
    "inputs",
    "inputs.base",
    "inputs.bindings",
    "inputs.keyboard",
    "inputs.hid",
    "ptt_controller",
    "debrief",
    "names",
]

# Read-only resource directories shipped as-is (inputs/ is the PTT source
# package; the input_sources.py shim stays in RUNTIME_MODULES).
RESOURCE_DIRS = ["lib", "data", "inputs"]

# Modules excluded from the core PyInstaller build (kept out of the light pack).
PYINSTALLER_EXCLUDES = ["tkinter", "matplotlib", "faster_whisper", "ctranslate2"]

# Optional voice-model directory (Piper). Copied when present; the pack is
# still valid without it (Piper simply reports unavailable).
OPTIONAL_RESOURCE_DIRS = ["piper_models"]

# Static files copied into the full pack.
STATIC_FILES = [".env.example", "README.md", "LICENSE", "FIRST_RUN.txt", "start.bat"]


def missing_modules() -> list:
    """Whitelist entries not on disk.

    iter_modules() silently skips them; a typo'd name in RUNTIME_MODULES would
    otherwise ship a pack with that module quietly missing.
    """
    return [name for name in RUNTIME_MODULES if not (ROOT / name).exists()]


def iter_modules():
    for name in RUNTIME_MODULES:
        if (ROOT / name).exists():
            yield name


def full_copy(dest: Path) -> int:
    """Copy the full-pack whitelist into ``dest``; return the file count."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for name in iter_modules():
        shutil.copy2(ROOT / name, dest / name)
        count += 1
    for d in RESOURCE_DIRS:
        src = ROOT / d
        if not src.is_dir():
            continue
        out = dest / d
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(src, out, ignore=shutil.ignore_patterns(
            "__pycache__", ".locks", "trees",
            ".agent_harnesses.json", "CACHEDIR.TAG", "*.cache"))
        count += sum(1 for _ in out.rglob("*") if _.is_file())
    for d in OPTIONAL_RESOURCE_DIRS:
        src = ROOT / d
        if not src.is_dir():
            continue
        out = dest / d
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__"))
        count += sum(1 for _ in out.rglob("*") if _.is_file())
    for f in STATIC_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, dest / f)
            count += 1
    return count


def pyinstaller_args() -> list:
    """The full PyInstaller argument list for the core (onedir) build.

    Emitted one arg per line so both build_release.ps1 and release.yml consume
    the SAME list instead of drifting (the CI build once lost --add-data data).
    """
    out = ["--noconfirm", "--clean", "--onedir", "--noupx",
           "--name", "F1Engineer", "--version-file", "version_info.txt",
           "--paths", "."]
    for m in PYINSTALLER_EXCLUDES:
        out += ["--exclude-module", m]
    if (ROOT / "data").is_dir():
        out += ["--add-data", "data;data"]
    for m in HIDDEN_IMPORT_MODULES:
        out += ["--hidden-import", m]
    out.append("FI.py")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="F1_TR build manifest helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("hidden-imports")
    sub.add_parser("pyinstaller-args")
    full = sub.add_parser("full-copy")
    full.add_argument("--dest", required=True)
    args = p.parse_args()

    missing = missing_modules()
    if missing:
        # A typo'd whitelist name must not silently ship a pack without it.
        print(f"WARNING: manifest whitelist entries missing on disk: {missing}",
              file=sys.stderr)

    if args.cmd == "list":
        for name in iter_modules():
            print(name)
    elif args.cmd == "hidden-imports":
        for name in HIDDEN_IMPORT_MODULES:
            print(name)
    elif args.cmd == "pyinstaller-args":
        for a in pyinstaller_args():
            print(a)
    elif args.cmd == "full-copy":
        n = full_copy(Path(args.dest))
        print(f"copied {n} files into {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
