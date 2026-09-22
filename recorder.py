"""Session recorder: persist telemetry-by-lap and Q&A to disk.

Writes one JSON file per run under ./sessions/, updated continuously so a
crash or a forgotten export still leaves usable data on disk.

Two record streams:
  - "laps": one entry per completed lap (lap time, sectors, fuel, tyre info)
  - "qa":   every (question, answer, tokens) turn

The file is rewritten atomically-ish on each change (small data, so cheap).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

SESSION_DIR = Path(__file__).parent / "sessions"


class SessionRecorder:
    """Append-only recorder for a single run."""

    def __init__(self, session_dir: Optional[Path] = None) -> None:
        self.dir = session_dir or SESSION_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = self.dir / f"session_{stamp}.json"
        self._lock = RLock()  # record_state holds it while calling _flush
        self._lap_keys = set()
        self.data: Dict[str, Any] = {
            "started": datetime.now().isoformat(timespec="seconds"),
            "laps": [],
            "qa": [],
            "session_uid": None,
            "track": None,
            "session_type": None,
            "final_leaderboard": [],
        }
        self._flush()

    # ---------------------------------------------------------------- laps

    def record_state(self, snapshot: Dict[str, Any]) -> None:
        """Pull new completed laps out of a state snapshot and store them.

        Uses the game's SESSION_HISTORY (authoritative, has all laps) plus the
        current lap snapshot for tyre/fuel context. Thread-safe: runs on the
        receiver thread and may overlap a record_qa on the HTTP thread.
        """
        with self._lock:
            self._record_state_locked(snapshot)

    def _record_state_locked(self, snapshot: Dict[str, Any]) -> None:
        latest = snapshot.get("latest", {})
        sess = latest.get("session", {})
        self.data["session_uid"] = snapshot.get("session", {}).get("session_uid")
        self.data["track"] = sess.get("track_id")
        self.data["session_type"] = sess.get("session_type")

        # Keep the latest full-field table (overwritten each poll).
        lb = snapshot.get("leaderboard")
        if lb:
            self.data["final_leaderboard"] = lb

        # Keep the latest vehicle status (damage + pit) for the report.
        dmg = latest.get("damage")
        lap_now = latest.get("lap", {})
        if dmg:
            self.data["vehicle_status"] = {
                "damage": dmg,
                "pit_status": lap_now.get("pit_status"),
                "num_pit_stops": lap_now.get("num_pit_stops"),
                "pit_limiter": latest.get("status", {}).get("pit_limiter"),
            }

        hist = latest.get("history", {})
        status = latest.get("status", {})
        car = latest.get("car", {})
        trends = snapshot.get("trends", {})

        changed = False
        recorded_nums = set(self._lap_keys)

        # Primary source: the game's SESSION_HISTORY (authoritative, all laps).
        for lap in hist.get("laps", []):
            key = lap.get("lap_num")
            if key is None or key in self._lap_keys:
                continue
            self._lap_keys.add(key)
            self.data["laps"].append({
                "lap_num": key,
                "lap_time_ms": lap.get("lap_time_ms"),
                "sector1_ms": lap.get("sector1_ms"),
                "sector2_ms": lap.get("sector2_ms"),
                "sector3_ms": lap.get("sector3_ms"),
                "valid": lap.get("valid"),
                "tyre_compound": status.get("tyre_compound_actual"),
                "tyres_age_laps": status.get("tyres_age_laps"),
                "fuel_in_tank_kg": status.get("fuel_in_tank_kg"),
                "tyre_temp_c": car.get("tyres_surface_temp_c"),
                "source": "session_history",
            })
            changed = True

        # Fallback: our own rolling lap-time trend, in case SESSION_HISTORY is
        # absent. We can't know lap numbers, so bucket by position.
        trend_laps = trends.get("lap_times_ms") or []
        for i, ms in enumerate(trend_laps):
            key = f"trend_{i}"
            if key in self._lap_keys or key in recorded_nums:
                continue
            # Skip if session_history already covered this lap count.
            if len(hist.get("laps", [])) >= i + 1:
                continue
            self._lap_keys.add(key)
            self.data["laps"].append({
                "lap_num": None,
                "lap_time_ms": ms,
                "source": "trend_fallback",
            })
            changed = True

        if changed:
            self._flush()

    # ------------------------------------------------------------------ qa

    def record_qa(self, question: str, answer: str,
                  usage: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self.data["qa"].append({
                "t": datetime.now().isoformat(timespec="seconds"),
                "q": question,
                "a": answer,
                "tokens": (usage or {}).get("total_tokens"),
            })
        self._flush()

    # --------------------------------------------------------------- files

    def _flush(self) -> None:
        with self._lock:  # RLock: re-entrant when called from record_state
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        # Also refresh the human-readable report alongside the JSON.
        try:
            from report_txt import render
            self.path.with_suffix(".txt").write_text(
                render(self.data), encoding="utf-8")
        except Exception:
            pass

    def summary_path(self) -> Path:
        return self.path
