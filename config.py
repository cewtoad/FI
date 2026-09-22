"""Single configuration entry for the F1 race engineer.

Replaces the scattered ``load_dotenv()`` calls. Two kinds of config live here:

  * static  - read once from ``.env`` / environment (.env wins over a stale
              process env is NOT the rule: environment wins, matching the
              previous behaviour of ``{**load_dotenv(), **os.environ}``).
  * runtime - LLM endpoint/model, active profile, selected audio devices.
              These can be changed while running and are persisted back to
              ``.env`` so the next launch remembers them.

All access is guarded by a lock so the HTTP thread can hot-swap values while
the voice/receiver threads read them.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from paths import app_root

_ENV_PATH = app_root() / ".env"


def parse_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """Read a simple KEY=VALUE .env file (no external dependency).

    Uses utf-8-sig so a BOM (e.g. from Windows Notepad) does not corrupt the
    first key. Also strips surrounding single/double quotes from values.
    """
    env: Dict[str, str] = {}
    p = path or _ENV_PATH
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip().lstrip("\ufeff")] = v
    return env


def load_dotenv(path: Optional[Path] = None) -> Dict[str, str]:
    """Backwards-compatible alias used by older modules."""
    return parse_env_file(path)


class Config:
    """Thread-safe config store with a small runtime-mutable overlay."""

    # Keys that may be changed at runtime and written back to .env.
    RUNTIME_KEYS = frozenset({
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
        "PROFILE",
        "AUDIO_INPUT", "AUDIO_OUTPUT",
    })

    def __init__(self, env_path: Optional[Path] = None) -> None:
        self._env_path = env_path or _ENV_PATH
        self._lock = threading.RLock()
        self._overlay: Dict[str, str] = {}
        self._env = self._read()

    # ------------------------------------------------------------ reading

    def _read(self) -> Dict[str, str]:
        return {**parse_env_file(self._env_path), **os.environ}

    def get(self, key: str, default: str = "") -> str:
        """Read a value. Runtime overlay wins, then live os.environ, then .env.

        Live os.environ is consulted each call so tests (and launchers) that
        set a variable after import are honoured, matching the previous
        ``{**load_dotenv(), **os.environ}`` precedence.
        """
        with self._lock:
            if key in self._overlay:
                return self._overlay[key]
            if key in os.environ:
                return os.environ[key]
            return self._env.get(key, default)

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def as_dict(self) -> Dict[str, str]:
        with self._lock:
            return {**self._env, **self._overlay}

    # ------------------------------------------------------------ writing

    def set_runtime(self, key: str, value: str, persist: bool = True) -> None:
        """Change a runtime key. Persists to .env when requested."""
        if key not in self.RUNTIME_KEYS:
            raise KeyError(f"not a runtime key: {key}")
        with self._lock:
            self._overlay[key] = value
            self._env[key] = value
        if persist:
            self._persist({key: value})

    def _persist(self, updates: Dict[str, str]) -> None:
        """Rewrite the .env file with the updated keys (creates if missing)."""
        lines: list = []
        if self._env_path.exists():
            lines = self._env_path.read_text(encoding="utf-8-sig").splitlines()
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    out.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        try:
            with self._lock:
                self._env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        except OSError:
            pass  # read-only filesystem: runtime change still applies in-memory


# Process-wide default instance used by the app. Tests may build their own.
_CONFIG: Optional[Config] = None
_CONFIG_LOCK = threading.Lock()


def get_config() -> Config:
    global _CONFIG
    with _CONFIG_LOCK:
        if _CONFIG is None:
            _CONFIG = Config()
        return _CONFIG
