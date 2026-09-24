"""Normalise the embedded Python ``._pth`` for the full pack (T10b).

The python.org embeddable distribution ships ``pythonXY._pth`` that puts only
``pythonXY.zip`` and ``.`` on sys.path and disables ``import site``. For our
full pack the app modules (``config.py``, ``lib/`` ...) live NEXT TO python.exe,
i.e. ``.`` is already correct. This tool:

  * ensures ``.`` is present (so ``import config`` / ``import lib.*`` work);
  * keeps ``import site`` disabled (no site-packages in the pack);
  * is idempotent and pure-text so it is unit-testable.

Usage:
    py -3.12 tools/fix_embedded_pth.py python-embed/python312._pth
"""

from __future__ import annotations

import sys
from pathlib import Path


def normalise_pth(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    entries = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    has_dot = any(e.strip() == "." for e in entries)
    out = []
    for ln in entries:
        if ln.strip().startswith("import "):
            continue
        out.append(ln)
    if not has_dot:
        out.append(".")
    out.append("")
    out.append("# import site disabled: the full pack adds stt_lib at runtime.")
    out.append("#import site")
    return "\n".join(out) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: fix_embedded_pth.py PATH_TO._pth", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 1
    path.write_text(normalise_pth(path.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"normalised {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
