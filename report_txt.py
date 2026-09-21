"""Human-readable TXT report from a recorded session JSON.

Reads a session_*.json written by recorder.py and renders a plain-text report
with a lap table, leaderboard, vehicle status and the Q&A log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def _fmt_ms(ms) -> str:
    if not ms or ms <= 0:
        return "-"
    ms = int(ms)
    m = ms // 60000
    r = ms % 60000
    return f"{m}:{r//1000:02d}.{r%1000:03d}" if m else f"{r/1000:.3f}"


def _gap(ms) -> str:
    if not ms or ms <= 0:
        return "-"
    return f"+{ms/1000:.3f}s"


def _r(v, nd: int = 1):
    """Round a numeric value for display, or '-' if None."""
    if v is None:
        return "-"
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def render(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    L = lines.append

    L("=" * 64)
    L(f"  F1 遥测报告    {data.get('started', '')}")
    L("=" * 64)
    L(f"  赛道: {data.get('track') or '-'}    类型: {data.get('session_type') or '-'}")
    L(f"  Session UID: {data.get('session_uid')}")
    L("")

    # ---- laps ----
    laps = data.get("laps", [])
    L("-" * 64)
    L(f"  圈速记录  (共 {len(laps)} 条)")
    L("-" * 64)
    if not laps:
        L("  (无圈速数据)")
    else:
        L(f"  {'圈':>4}  {'圈速':>10}  {'S1':>8}  {'S2':>8}  {'S3':>8}  轮胎  胎龄  有效")
        best = None
        best_lap = None
        for e in laps:
            lt = e.get("lap_time_ms")
            if lt and (best is None or lt < best):
                best, best_lap = lt, e.get("lap_num")
            L(f"  {str(e.get('lap_num') or '-'):>4}  "
              f"{_fmt_ms(lt):>10}  "
              f"{_fmt_ms(e.get('sector1_ms')):>8}  "
              f"{_fmt_ms(e.get('sector2_ms')):>8}  "
              f"{_fmt_ms(e.get('sector3_ms')):>8}  "
              f"{str(e.get('tyre_compound') or '-'):<4}  "
              f"{str(e.get('tyres_age_laps') if e.get('tyres_age_laps') is not None else '-'):>4}  "
              f"{'是' if e.get('valid') else '否'}")
        if best_lap is not None:
            L("")
            L(f"  最快圈: 第 {best_lap} 圈  {_fmt_ms(best)}")
    L("")

    # ---- leaderboard ----
    lb = data.get("final_leaderboard") or []
    if lb:
        L("-" * 64)
        L(f"  全场排名  (共 {len(lb)} 车)")
        L("-" * 64)
        L(f"  {'P':>3}  {'车手':<16}  {'轮胎':<6}  {'胎龄':>4}  {'落后领先':>9}")
        for r in lb:
            mark = "*" if r.get("is_player") else " "
            gap_ms = r.get("gap_to_leader_ms") or 0
            gap = "领先" if r["position"] == 1 else f"+{gap_ms/1000:.3f}s"
            tyre = str(r.get("tyre") or "-")
            age = r.get("tyre_age")
            L(f"  {r['position']:>3}  {mark}{str(r.get('driver') or '-'):<15}  "
              f"{tyre:<6}  {str(age if age is not None else '-'):>4}  {gap:>9}")
        L("")

    # ---- vehicle status (damage + pit) ----
    vs = data.get("vehicle_status") or {}
    dmg = vs.get("damage") or {}
    if dmg:
        L("-" * 64)
        L("  车辆状态")
        L("-" * 64)
        L(f"  进站次数: {vs.get('num_pit_stops', 0)}    "
          f"进站状态: {vs.get('pit_status') or 'NONE'}    "
          f"限速器: {'开' if vs.get('pit_limiter') else '关'}")
        L("")
        L("  车损 (%):")
        L(f"    前翼左 {dmg.get('front_left_wing')}  前翼右 {dmg.get('front_right_wing')}  "
          f"尾翼 {dmg.get('rear_wing')}  底板 {dmg.get('floor')}  侧箱 {dmg.get('sidepod')}")
        L(f"    引擎 {dmg.get('engine')}  变速箱 {dmg.get('gearbox')}  "
          f"DRS故障 {'是' if dmg.get('drs_fault') else '否'}  "
          f"ERS故障 {'是' if dmg.get('ers_fault') else '否'}")
        L("  轮胎磨损 (%):")
        L(f"    FL {_r(dmg.get('tyre_wear_fl'))}  FR {_r(dmg.get('tyre_wear_fr'))}  "
          f"RL {_r(dmg.get('tyre_wear_rl'))}  RR {_r(dmg.get('tyre_wear_rr'))}")
        L("")

    # ---- qa ----
    qa = data.get("qa", [])
    L("-" * 64)
    L(f"  对话记录  (共 {len(qa)} 条)")
    L("-" * 64)
    if not qa:
        L("  (无对话记录)")
    else:
        for t in qa:
            ts = (t.get("t") or "")[-8:]
            L(f"  [{ts}]  你: {t.get('q')}")
            L(f"            AI: {t.get('a')}")
            if t.get("tokens"):
                L(f"            ({t['tokens']} tokens)")
            L("")

    return "\n".join(lines)


def render_file(json_path: Path) -> Path:
    """Render a session JSON to a sibling .txt file and return its path."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    txt = render(data)
    out = Path(json_path).with_suffix(".txt")
    out.write_text(txt, encoding="utf-8")
    return out
