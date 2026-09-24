"""Pytest bootstrap for the F1_TR test suite.

The tests all live under ``tests/`` but import the project modules that live
in the repository root. Rather than turning the project into an installable
package, we simply put the repo root on ``sys.path`` before collection.

Also registers the ``network`` marker so tests that need loopback UDP / audio
can be skipped by default (``RUN_NETWORK_TESTS=1`` opts in).
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
        "network: needs loopback UDP / audio; set RUN_NETWORK_TESTS=1",
    )
