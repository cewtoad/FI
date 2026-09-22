"""Single place for audio-device resolution.

Replaces the three ad-hoc ``_device_index()`` copies that each matched a device
name fragment and silently fell back to the system default on a miss (which
bled the engineer's answer out of the speakers during a race).

Public API:
    list_devices(kind)     -> [{id,name,channels,default}]
    resolve(fragment, kind)-> int | None  (unique fuzzy match)
    default_device(kind)   -> int | None  (system default, re-queried per call)
    active_device(kind)    -> {id,name,source}  (what a take started NOW uses)
    current()              -> {"input": str, "output": str}  (configured pins)
    set_device(kind, frag) -> str  ("" un-pins -> follow the system default)

Device policy for "works on anyone's machine": when nothing is pinned, every
record/play resolves the system's *current* default device fresh, so swapping
headsets or switching the Windows default mid-session is picked up by the next
take. A pinned fragment (AUDIO_INPUT/AUDIO_OUTPUT) still wins while it matches;
if it goes missing we warn loudly and follow the default for that take.


sounddevice is imported lazily so the panel/web UI runs without it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def default_device(kind: str = "input") -> Optional[int]:
    """The system's current default device id for the direction, or None.

    Re-queried on every call - this is what makes device switching live.
    """
    if kind not in _KIND_KEY:
        raise ValueError("kind must be 'input' or 'output'")
    try:
        sd = _sd()
        idx = sd.default.device[0 if kind == "input" else 1]
        if idx is not None and idx >= 0:
            return int(idx)
    except Exception:
        pass
    return None


def _device_name(kind: str, idx: int) -> str:
    for d in list_devices(kind):
        if d["id"] == idx:
            return d["name"]
    return f"device #{idx}"


def active_device(kind: str, fragment: Optional[str] = None) -> Dict[str, Optional[object]]:
    """Return the device a record/play started RIGHT NOW would use.

    Precedence: explicit ``fragment`` argument (e.g. a CLI override) that
    matches, then the configured pin (AUDIO_INPUT/AUDIO_OUTPUT), then the
    system's current default device. The default is re-queried per call, so
    "different person, different headset" needs no configuration at all.

    Returns {"id": int|None, "name": str|None, "source": str, "note": str}
    where source is "pinned" | "default" | "none". A pinned fragment that no
    longer matches falls back to the default for this take, with a loud
    warning - never a silent wrong-device recording.
    """
    if kind not in _KIND_KEY:
        raise ValueError("kind must be 'input' or 'output'")
    pinned = fragment if fragment is not None else get_config().get(_KIND_KEY[kind]).strip()
    note = ""
    if pinned:
        idx = resolve(pinned, kind)
        if idx is not None:
            return {"id": idx, "name": _device_name(kind, idx), "source": "pinned", "note": note}
        note = f"指定的设备 '{pinned}' 不存在，本次改用系统当前设备"
        print(f"[audio] 找不到{kind}设备 '{pinned}'，本次跟随系统当前设备。"
              f"可用设备: " + (", ".join(d["name"] for d in list_devices(kind)) or "(none)"),
              flush=True)
    idx = default_device(kind)
    if idx is not None:
        return {"id": idx, "name": _device_name(kind, idx), "source": "default", "note": note}
    return {"id": None, "name": None, "source": "none", "note": note}


def describe() -> Dict[str, Any]:
    """Both the configured pins and the devices actually in use right now."""
    cur = current()
    return {
        "configured": cur,
        "active": {
            "input": active_device("input"),
            "output": active_device("output"),
        },
    }


def set_device(kind: str, fragment: str, persist: bool = True) -> str:
    """Pin a device by name fragment; an empty fragment un-pins it
    (i.e. go back to following the system's current device)."""
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
