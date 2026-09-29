"""Pytest bootstrap for the F1_TR test suite.

The tests all live under ``tests/`` but import the project modules that live
in the repository root. Rather than turning the project into an installable
package, we simply put the repo root on ``sys.path`` before collection.

Pure loopback UDP tests (udp / parse_guard / full_flow) run by default — they
need no audio or hardware and are CI-safe. Only the ``network`` marker (audio
tests such as the TTS smoke test) is skipped unless RUN_NETWORK_TESTS=1.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "network: needs a real audio device; set RUN_NETWORK_TESTS=1",
    )
