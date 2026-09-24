"""Driver name rendering: Chinese / English / car number (T6.3).

Loads the seed table from ``data/driver_names.json`` (via resource_root so it
works from a packaged build too) and renders a name for a driver by code or
car index. Unknown drivers fall back to the original name or "carN".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("f1_tr.names")


def _load_seed() -> List[Dict[str, Any]]:
    try:
        from paths import resource_root
        path = resource_root() / "data" / "driver_names.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("drivers", [])
    except Exception as e:  # noqa: BLE001
        _log.debug("driver_names.json load failed: %r", e)
    return []


class NameRenderer:
    """Render driver names in the configured style."""

    def __init__(self, style: str = "zh",
                 drivers: Optional[List[Dict[str, Any]]] = None) -> None:
        self.style = (style or "zh").lower()
        self._drivers = drivers if drivers is not None else _load_seed()
        self._by_code = {}
        self._by_num = {}
        for d in self._drivers:
            code = str(d.get("code", "")).upper()
            if code:
                self._by_code[code] = d
            if d.get("num") is not None:
                self._by_num[int(d["num"])] = d

    # -- lookup helpers ---------------------------------------------------

    def _lookup(self, token: Any) -> Optional[Dict[str, Any]]:
        if token is None:
            return None
        if isinstance(token, int):
            return self._by_num.get(token)
        text = str(token).strip()
        if not text:
            return None
        return self._by_code.get(text.upper())

    def render(self, token: Any, style: Optional[str] = None) -> str:
        """Render a driver identified by code (str) or race number (int).

        ``token`` may also be a pre-resolved name string (led through as-is).
        """
        style = (style or self.style).lower()
        info = self._lookup(token)
        if info is None:
            # Not in the table: pass through the token (may already be a name).
            return str(token) if token is not None else "?"
        if style == "number":
            return str(info.get("num", info.get("code")))
        if style == "en":
            return str(info.get("code", info.get("zh")))
        return str(info.get("zh", info.get("code")))

    def name_from_index(self, car_index: int, participants: Dict[int, Dict[str, Any]],
                        style: Optional[str] = None) -> str:
        """Render using the participant's name/race number first, then seed."""
        pinfo = (participants or {}).get(car_index)
        if pinfo:
            num = pinfo.get("race_number")
            info = self._lookup(num) if num is not None else None
            if info is None:
                info = self._lookup(pinfo.get("name"))
            if info is not None:
                return self.render(info.get("code"), style)
            if pinfo.get("name"):
                return str(pinfo["name"])
        return f"car{car_index}"
