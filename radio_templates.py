"""Chinese templates for proactive radio (T5.9).

Every template is keyed by its rule id and rendered with ``render(id, **kw)``.
Hard rule: NO imperative pit instructions ("进站吧"/"Box"/...) - the engineer
reports windows and consequences only, never issues the instruction. The lint
test in tests/test_radio_templates.py enforces this.
"""

from __future__ import annotations

from typing import Any, Dict

# id -> template string. Placeholders use str.format named fields.
TEMPLATES: Dict[str, str] = {
    # ---- P0 safety ----
    "sc_deployed": "安全车出动，{name}期间注意控速。",
    "vsc_deployed": "虚拟安全车，保持节奏。",
    "sc_ending": "安全车即将结束，准备重新起步。",
    "red_flag": "红旗，减速回到维修区。",
    "engine_failure": "引擎出现故障，注意动力。",
    "major_damage": "车损已达 {pct}%，下压力会受影响。",
    "drs_fault": "DRS 故障。",
    "ers_fault": "ERS 故障。",
    "wrong_way": "警告，方向反了。",
    "penalty_issued": "你被罚时 {seconds} 秒。",
    "tyre_puncture": "疑似爆胎，注意转向。",
    "player_retired": "本车已退赛。",
    # ---- P1 strategy ----
    "pit_window_open": ("进站窗口打开：理想第 {ideal} 圈，最晚第 {latest} 圈，"
                        "预计出站第 {rejoin} 位。"),
    "pit_window_last": "本圈是进站窗口最后一圈（第 {latest} 圈）。",
    "pit_window_missed": "已错过计划进站窗口（最晚第 {latest} 圈）。",
    "pit_sc_opportunity": ("当前安全车，进站窗口内（理想第 {ideal} 圈），"
                           "出站预计第 {rejoin} 位。"),
    "fuel_deficit": "油量不足：按当前消耗完赛缺 {laps:.2f} 圈。",
    "tyre_critical": "轮胎接近极限：预计还能跑 {laps:.0f} 圈。",
    "tyre_attention": "轮胎需要留意：预计还能跑 {laps:.0f} 圈。",
    "rain_incoming": "预计 {eta:.0f} 分钟后降雨，降水概率 {pct}%。",
    "track_limits_warning": "赛道限制警告，已累计 {count} 次。",
    "unserved_penalty": "有未执行的处罚（{kind}）。",
    "undercut_risk": "后车 {name} 已进站，存在 undercut 风险。",
    "yellow_ahead": "前方有黄旗区域{zone}。",
    # ---- P2 info ----
    "position_change": "{text}",
    "fastest_lap_you": "最快圈是你：{time}。",
    "gap_report": "距前车 {ahead}，距后车 {behind}。",
    "lap_summary_chatty": "第 {lap} 圈，{last}，位置 P{pos}。",
    "overtake_range_enter": "已进入超车范围，距前车 {gap}。",
    "overtake_range_exit": "已离开超车范围。",
    "retirement_other": "{name} 退赛。",
    "final_lap": "最后一圈。",
    "chequered": "方格旗，最终第 {pos} 位。",
    "lights_out": "起步。",
    # ---- qualifying ----
    "quali_out_lap_start": "出站圈开始。",
    "quali_flying_lap": "进入飞驰圈。",
    "quali_time_300": "排位剩余 5 分钟。",
    "quali_time_120": "排位剩余 2 分钟。",
    "quali_time_60": "排位剩余 1 分钟。",
    "quali_lap_done": "第 {lap} 圈 {time}，暂时 P{pos}，与最快差 {delta}。",
    "quali_traffic": "后方 {name} 可能是快圈，注意让车。",
    "quali_invalid_lap": "本圈无效（{reason}）。",
    # ---- practice ----
    "practice_long_run": "长距离平均 {avg}，衰退 {deg:.3f} 秒/圈。",
    "practice_stint_end": "上一段平均值 {avg}，共 {laps} 圈。",
    # ---- time trial ----
    "tt_lap_delta": "本圈 {delta}。",
    "tt_new_pb": "新个人最快圈：{time}。",
    # ---- system ----
    "quiet_mode_on": "好的，安静模式。",
    "quiet_mode_off": "恢复播报。",
    "quiet_locked": "当前由设置锁定。",
    "answer": "{text}",
}


def render(template_id: str, **kwargs: Any) -> str:
    """Render a template; unknown ids return an empty string."""
    tmpl = TEMPLATES.get(template_id)
    if tmpl is None:
        return ""
    try:
        return tmpl.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # Missing placeholder: return the raw template rather than crashing the
        # radio; the lint/test coverage keeps templates and callers aligned.
        return tmpl
