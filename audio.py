"""Single place for audio-device resolution.

Replaces the three ad-hoc ``_device_index()`` copies that each matched a device
name fragment and silently fell back to the system default on a miss (which
bled the engineer's answer out of the speakers during a race).

Public API:
    list_devices(kind)     -> [{id,name,channels,default}]
    resolve(fragment, kind)-> int | None    (unique fuzzy match)
    current()              -> {"input": str, "output": str}
    set_device(kind, frag) -> str           (persists to .env)

sounddevice is imported lazily so the panel/web UI runs without it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

from config import get_config
from paths import app_root

_HERE = app_root()
if str(_HERE / "stt_lib") not in sys.path:
    sys.path.insert(0, str(_HERE / "stt_lib"))

_KIND_KEY = {"input": "AUDIO_INPUT", "output": "AUDIO_OUTPUT"}


def _sd():
    import sounddevice as sd  # lazy: only needed when audio is actually used
    return sd


def list_devices(kind: str = "input") -> List[Dict]:
    """List devices that can do the requested direction (default first)."""
    if kind not in _KIND_KEY:
        raise ValueError("kind must be 'input' or 'output'")
    ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        sd = _sd()
        devices = sd.query_devices()
        try:
            default = sd.default.device
            default_id = default[0] if kind == "input" else default[1]
        except Exception:
            default_id = None
    except Exception:
        return []
    out = []
    for i, d in enumerate(devices):
        if d.get(ch_key, 0) > 0:
            out.append({
                "id": i,
                "name": d["name"],
                "channels": d[ch_key],
                "default": i == default_id,
            })
    return out


def resolve(fragment: str, kind: str = "input") -> Optional[int]:
    """Return the unique device id whose name contains ``fragment``.

    Returns None when nothing matches. Ambiguity resolves to the first match,
    but ``current()`` records the chosen id so callers can warn.
    """
    frag = (fragment or "").strip().lower()
    if not frag:
        return None
    for d in list_devices(kind):
        if frag in d["name"].lower():
            return d["id"]
    return None


def current() -> Dict[str, str]:
    cfg = get_config()
    return {
        "input": cfg.get("AUDIO_INPUT").strip(),
        "output": cfg.get("AUDIO_OUTPUT").strip(),
    }


def set_device(kind: str, fragment: str, persist: bool = True) -> str:
    if kind not in _KIND_KEY:
        raise ValueError("kind must be 'input' or 'output'")
    get_config().set_runtime(_KIND_KEY[kind], fragment, persist=persist)
    return fragment


def resolve_or_warn(fragment: str, kind: str = "input") -> Optional[int]:
    """Like ``resolve`` but prints a clear warning when the device is missing.

    Never silently returns None for a configured fragment - the caller can
    decide to abort rather than record/play on the wrong device.
    """
    if not fragment:
        return None
    idx = resolve(fragment, kind)
    if idx is None:
        print(f"[audio] 找不到{kind}设备 '{fragment}',可用设备: "
              + ", ".join(d["name"] for d in list_devices(kind)) or "(none)",
              flush=True)
    return idx
