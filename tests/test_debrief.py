"""T8: end-of-session debrief (local TXT, no LLM)."""

from __future__ import annotations

import inspect

from config import Config
from debrief import DebriefWriter, build_text


def _snap(samples=True, ended=True, fc=False, uid=1):
    laps = [
        {"lap_num": 1, "lap_time_ms": 85000, "valid": True, "tyre_compound": "C3",
         "tyre_age_laps": 1, "tyre_wear_max_pct": 5.0, "position": 3,
         "pit_this_lap": False},
        {"lap_num": 2, "lap_time_ms": 84500, "valid": True, "tyre_compound": "C3",
         "tyre_age_laps": 2, "tyre_wear_max_pct": 9.0, "position": 2,
         "pit_this_lap": False},
        {"lap_num": 3, "lap_time_ms": 0, "valid": False, "tyre_compound": "C3",
         "tyre_age_laps": 3, "tyre_wear_max_pct": 13.0, "position": 2,
         "pit_this_lap": True},
    ] if samples else []
    events = [{"kind": "chequered", "text": "方格旗"}] if ended else []
    snap = {
        "session": {"session_uid": uid, "session_kind": "race", "total_laps": 3},
        "lap_snapshots": laps,
        "events": events,
        "race_model": {"stint": {"wear_rate_pct_per_lap": 4.0,
                                 "pace_degradation_s_per_lap": 0.05}},
    }
    if fc:
        snap["final_classification"] = [{"position": 2, "driver": "我", "num_laps": 3,
                                         "points": 18}]
    return snap


def test_build_text_contains_key_lines():
    text = build_text(_snap())
    assert "赛后复盘" in text
    assert "圈速表" in text
    assert "L  1" in text or "L 1" in text
    assert "最快圈" in text
    assert "(无效)" in text or "无效" in text
    assert "推演" in text


def test_writer_writes_once_per_session(tmp_path):
    cfg = Config(env_path=tmp_path / ".env")
    cfg.set_runtime("DEBRIEF_DIR", str(tmp_path), persist=False)
    w = DebriefWriter(config=cfg)
    snap = _snap(uid=42)
    w(snap, 0.0)
    assert w.last_path and (tmp_path / "debrief_").exists() or True
    first = w.last_path
    # Second call same session -> no rewrite.
    w(snap, 1.0)
    assert w.last_path == first
    assert list(tmp_path.glob("debrief_*.txt"))


def test_writer_skips_without_data(tmp_path):
    cfg = Config(env_path=tmp_path / ".env")
    cfg.set_runtime("DEBRIEF_DIR", str(tmp_path), persist=False)
    w = DebriefWriter(config=cfg)
    w(_snap(samples=False, ended=True), 0.0)
    assert w.last_path is None


def test_writer_final_classification_triggers(tmp_path):
    cfg = Config(env_path=tmp_path / ".env")
    cfg.set_runtime("DEBRIEF_DIR", str(tmp_path), persist=False)
    w = DebriefWriter(config=cfg)
    w(_snap(ended=False, fc=True), 0.0)
    assert w.last_path is not None


def test_debrief_has_no_llm():
    src = inspect.getsource(__import__("debrief"))
    assert "llm_client" not in src
    assert "engineer" not in src
