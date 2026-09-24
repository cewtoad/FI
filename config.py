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

import config_schema
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


def _to_env_str(value: Any) -> str:
    """Serialise a coerced setting value for the .env file."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class Config:
    """Thread-safe config store with a small runtime-mutable overlay."""

    # Keys that may be changed at runtime and written back to .env. Derived
    # from the schema (T3.2); the original six keys remain runtime.
    RUNTIME_KEYS = config_schema.runtime_keys()

    def __init__(self, env_path: Optional[Path] = None) -> None:
        self._env_path = env_path or _ENV_PATH
        self._lock = threading.RLock()
        self._overlay: Dict[str, str] = {}
        self._env = self._read()
        # T3.2: last persist failure (None = ok). Written under the lock.
        self.last_persist_error: Optional[str] = None
        self._env_mtime: Optional[float] = self._current_mtime()

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

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, "1" if default else "0").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
        return default

    def as_dict(self) -> Dict[str, str]:
        with self._lock:
            return {**self._env, **self._overlay}

    # ------------------------------------------------------------ writing

    def set_runtime(self, key: str, value: str, persist: bool = True) -> None:
        """Change a runtime key. Persists to .env when requested.

        Validates the value against the schema (type/range/choices) and raises
        ValueError on a bad value (the previous code only checked the key).
        """
        if key not in self.RUNTIME_KEYS:
            raise KeyError(f"not a runtime key: {key}")
        coerced = config_schema.validate(key, value)
        stored = _to_env_str(coerced)
        with self._lock:
            self._overlay[key] = stored
            self._env[key] = stored
        if persist:
            self._persist({key: stored})

    def _persist(self, updates: Dict[str, str]) -> None:
        """Rewrite the .env file with the updated keys (atomic, T3.2).

        Writes to a temp file then os.replace()s it into place so a crash or a
        concurrent reader never sees a half-written .env. Failures are recorded
        in ``last_persist_error`` instead of being swallowed silently.
        """
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
                tmp = self._env_path.with_suffix(self._env_path.suffix + ".tmp")
                tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                os.replace(tmp, self._env_path)
                self._env_mtime = self._current_mtime()
                self.last_persist_error = None
        except OSError as e:
            # Read-only filesystem: the change still applies in memory.
            with self._lock:
                self.last_persist_error = f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------- file watch

    def _current_mtime(self) -> Optional[float]:
        try:
            return self._env_path.stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Re-read .env if its mtime changed. Returns True when reloaded.

        File values win over the overlay for keys the file now defines (avoids
        a long-running process serving a stale value after the config page,
        which is a separate process, rewrote .env).
        """
        mtime = self._current_mtime()
        with self._lock:
            if mtime is None or mtime == self._env_mtime:
                return False
            self._env = self._read()
            self._env_mtime = mtime
            # Drop overlay entries the file now defines (file wins).
            for key in list(self._overlay):
                if key in self._env:
                    del self._overlay[key]
        return True

    def is_writable(self) -> bool:
        """True when the .env file (or its directory) can be written."""
        try:
            if self._env_path.exists():
                with open(self._env_path, "a", encoding="utf-8"):
                    pass
            else:
                probe = self._env_path.parent / ".f1tr_write_probe"
                probe.write_text("", encoding="utf-8")
                probe.unlink()
            return True
        except OSError:
            return False


# Process-wide default instance used by the app. Tests may build their own.
_CONFIG: Optional[Config] = None
_CONFIG_LOCK = threading.Lock()


def get_config() -> Config:
    global _CONFIG
    with _CONFIG_LOCK:
        if _CONFIG is None:
            _CONFIG = Config()
        return _CONFIG
