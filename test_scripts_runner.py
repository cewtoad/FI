"""Run the project's script-style test modules under pytest.

The repo grew a set of ``tests_*.py`` scripts run by hand (``py tests_fuel.py``)
rather than as pytest functions. This module imports each and runs its
``main()`` so a single ``pytest`` invocation executes the whole offline suite.

Live/network scripts are skipped by default unless RUN_NETWORK_TESTS=1:
  - tests_udp.py        (opens a UDP socket / needs loopback)
  - tests_parse_guard.py(opens a UDP socket)
  - tests_full.py       (drives a live UDP receiver task)
  - tests_tts_smoke.py  (needs Windows SAPI / audio)
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os

import pytest

SKIP_NETWORK = {
    "tests_udp", "tests_parse_guard", "tests_full", "tests_tts_smoke",
}

SCRIPTS = [
    "tests_2026",
    "tests_ai",
    "tests_damage",
    "tests_engineer",
    "tests_event_ovtk",
    "tests_fuel",
    "tests_invalid_lap",
    "tests_leaderboard",
    "tests_offline",
    "tests_overtake",
    "tests_packet_filter",
    "tests_recorder",
    "tests_spectate",
    "tests_voice",
]


def _run(module_name: str) -> None:
    if module_name in SKIP_NETWORK and os.environ.get("RUN_NETWORK_TESTS") != "1":
        pytest.skip(f"{module_name} needs network/audio (set RUN_NETWORK_TESTS=1)")
    mod = importlib.import_module(module_name)
    main = getattr(mod, "main", None)
    if main is None:
        pytest.skip(f"{module_name} has no main()")
    result = main()
    if inspect.isawaitable(result):
        asyncio.run(result)


@pytest.mark.parametrize("module_name", SCRIPTS)
def test_script(module_name: str) -> None:
    _run(module_name)
