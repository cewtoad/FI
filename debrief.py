"""End-of-session debrief (T8).

Pure local: when the session ends (CHEQUERED / SESSION_ENDED /
FINAL_CLASSIFICATION) and we have at least one lap, write a plain-text report
to DEBRIEF_DIR (default app_root()/sessions). No LLM is involved.

``DebriefWriter`` is a ticker handler: it inspects each snapshot, detects the
end signal, and writes the file once per session.
"""

from __future__ import annotations

import logging
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("f1_tr.debrief")

_END_KINDS = {"chequered", "session_ended"}


def _fmt_ms(ms) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return "-"
    m, rem = divmod(ms, 60000)
    s, milli = divmod(rem, 1000)
    return f"{m}:{s:02d}.{milli:03d}" if m else f"{s}.{milli:03d}"


def _stints(samples: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split lap samples into stints by compound change / age reset."""
    stints: List[List[Dict[str, Any]]] = []
    for s in samples:
        if not stints:
            stints.append([s])
            continue
        prev = stints[-1][-1]
        new = (s.get("tyre_compound") != prev.get("tyre_compound")
               or (isinstance(s.get("tyre_age_laps"), int)
                   and isinstance(prev.get("tyre_age_laps"), int)
                   and s["tyre_age_laps"] <= prev["tyre_age_laps"]))
        if new:
            stints.append([s])
        else:
            stints[-1].append(s)
    return stints


def build_text(snapshot: Dict[str, Any], race_model: Optional[Dict[str, Any]] = None) -> str:
    if race_model is None:
        race_model = snapshot.get("race_model")
    samples: List[Dict[str, Any]] = snapshot.get("lap_snapshots") or []
    session = snapshot.get("session", {}) or {}
    lines: List[str] = []
    lines.append("=" * 48)
    lines.append("  F1 Race Engineer · 赛后复盘")
    lines.append("=" * 48)
    lines.append(f"会话类型: {session.get('session_kind') or session.get('session_type')}")
    lines.append(f"总圈数: {session.get('total_laps')}")
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    valid = [s for s in samples if s.get("valid") and s.get("lap_time_ms")]
    lines.append("-- 圈速表 --")
    for s in samples:
        mark = "" if s.get("valid") else " (无效)"
        pit = " [进站]" if s.get("pit_this_lap") else ""
        lines.append(f"  L{s.get('lap_num'):>3}  {_fmt_ms(s.get('lap_time_ms'))}"
                     f"  P{s.get('position')}{mark}{pit}")
    lines.append("")

    if valid:
        times = [s["lap_time_ms"] for s in valid]
        best = min(times)
        lines.append("-- 汇总 --")
        lines.append(f"  最快圈: {_fmt_ms(best)}")
        lines.append(f"  平均圈: {_fmt_ms(int(sum(times) / len(times)))}")
        try:
            sd = statistics.pstdev(times)
            lines.append(f"  稳定性(std): {sd/1000:.3f}s")
        except Exception:
            pass
        lines.append("")

    lines.append("-- 分段(stint) --")
    for i, stint in enumerate(_stints(samples), 1):
        comp = stint[0].get("tyre_compound")
        laps = len(stint)
        st_valid = [s["lap_time_ms"] for s in stint if s.get("valid") and s.get("lap_time_ms")]
        avg = _fmt_ms(int(sum(st_valid) / len(st_valid))) if st_valid else "-"
        wear = [s.get("tyre_wear_max_pct") for s in stint if s.get("tyre_wear_max_pct") is not None]
        wear_txt = f"{wear[-1]:.0f}%" if wear else "-"
        lines.append(f"  第{i}段 {comp}: {laps} 圈, 均值 {avg}, 末段磨损 {wear_txt}")
    lines.append("")

    if race_model is not None:
        st = race_model.get("stint")
        if st:
            lines.append("-- 推演 --")
            lines.append(f"  磨损速率: {st.get('wear_rate_pct_per_lap')}%/圈")
            lines.append(f"  配速衰退: {st.get('pace_degradation_s_per_lap')}s/圈")
            lines.append("")

    events = snapshot.get("events") or []
    if events:
        lines.append("-- 关键事件 --")
        for e in events:
            lines.append(f"  {e.get('text')}")
        lines.append("")

    fc = snapshot.get("final_classification") or []
    if fc:
        lines.append("-- 最终成绩 --")
        for row in fc:
            lines.append(f"  P{row.get('position')} {row.get('driver')} "
                         f"({row.get('num_laps')} 圈, {row.get('points')} 分)")
        lines.append("")
    return "\n".join(lines)


class DebriefWriter:
    """Ticker handler that writes one debrief per session (T8)."""

    def __init__(self, config: Any = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self._cfg = config
        self._log = logger or _log
        self._written_for_uid = None
        self.last_path: Optional[str] = None

    def _dir(self) -> Path:
        name = "sessions"
        if self._cfg is not None:
            try:
                name = self._cfg.get("DEBRIEF_DIR", "sessions") or "sessions"
            except Exception:
                pass
        from paths import app_root
        p = Path(name)
        if not p.is_absolute():
            p = app_root() / p
        return p

    def _enabled(self) -> bool:
        if self._cfg is None:
            return True
        try:
            return self._cfg.get_bool("DEBRIEF_ENABLE", True)
        except Exception:
            return True

    def __call__(self, snapshot: dict, now: float) -> None:
        if not self._enabled():
            return
        samples = snapshot.get("lap_snapshots") or []
        if not samples:
            return
        events = snapshot.get("events") or []
        ended = any(e.get("kind") in _END_KINDS for e in events)
        has_fc = bool(snapshot.get("final_classification"))
        if not (ended or has_fc):
            return
        uid = (snapshot.get("session", {}) or {}).get("session_uid")
        if uid == self._written_for_uid:
            return
        self._written_for_uid = uid
        try:
            self.write(snapshot)
        except Exception as e:  # noqa: BLE001 - a debrief failure must not crash
            self._log.warning("debrief write failed: %r", e)

    def write(self, snapshot: Dict[str, Any]) -> Path:
        out_dir = self._dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"debrief_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        text = build_text(snapshot, snapshot.get("race_model"))
        path.write_text(text, encoding="utf-8")
        self.last_path = str(path)
        self._log.info("debrief written to %s", path)
        return path
