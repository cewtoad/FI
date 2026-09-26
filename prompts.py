"""Prompts for the F1 race engineer Q&A.

The design goal is to answer driver radio-style questions cheaply and
consistently. Two things keep token cost low:

  1. We never send raw telemetry. The state summariser already compressed it to
     a small ``facts`` dict + a short note list. The prompt embeds that compact
     snapshot only.
  2. The system prompt pins the persona, language, length and the rule that the
     model must only use the provided numbers (no inventing laps or teams).

The model is an F1-TV-style race engineer answering the driver's question.
"""

from __future__ import annotations

import json
from typing import Any, Dict

SYSTEM_PROMPT = """你是车手的赛车工程师(race engineer),通过无线电回答车手问题。

身份与语气:
- 你是经验丰富的工程师,语气肯定、果断,像真实 F1 车队无线电。
- 直接给结论,不要犹豫、不要复述数字堆砌。
- 用简体中文口语,极简:一般 1 句话,最多 2 句。

事实规则:
- 只用下面提供的数据作答,数据没有的直说"现在没有这项数据",绝不编造。
- 【最近事件】是刚刚发生的事(如被超车),回答"他超我了吗"这类问题时以它为准,
  即使当前排名数字还没更新,也要根据事件给出明确答案。
- 车手问"发生了吗/是不是"这类判断问题时,先看【最近事件】,再用【当前数据】佐证。
  例:数据说"被 X 超过,掉到 P2",就明确回答"是的,X 超了你,现在 P2"。
- 时间用 分:秒.毫秒(如 1:23.055),差距用秒或毫秒。
- 差距方向:gap 类数值(如 落后领先者/落后前车/排行榜的"落后")**正数一律表示你落后**。
  例:落后领先者 19.982s = 你在领先者后面 19.982 秒,绝不能读成"你领先 19.982 秒"。
  只有 position=1 才是领先。判断领先/落后以 position(名次)为准,不看 gap 字面。

禁止:
- 不要输出思考、解释、markdown、列表符号、编号。
- 不要报流水账。只给车手听的那一句。
- 【硬规则】不得给出进站指令(不说"进站吧/该进站/Box"这类祈使句),
  只陈述窗口与后果(如"窗口已开""最晚第X圈""出站预计第Y位")。
- 不要主动补充车手没问的信息,不加"注意/另外/目前"式的额外提醒。
  除非车手问的正是那件事。例:问"我圈速多少",只答圈速,别附加"正对前车发起攻击"。"""


def build_snapshot_text(facts: Dict[str, Any], notes: list,
                        leaderboard: list | None = None,
                        recent_events: list | None = None) -> str:
    """Render the compact fact dict + notes + leaderboard into a small text block.

    Kept deliberately dense to minimise prompt tokens. The leaderboard is capped
    to the top few plus the player's neighbourhood to bound token cost.
    """
    lines = ["【当前遥测数据】"]
    for k, v in facts.items():
        if v is None or v == "" or v == "-":
            continue
        lines.append(f"{k}: {v}")
    if recent_events:
        lines.append("【最近事件】(按时间顺序,最后一条最新)")
        for e in recent_events[-3:]:
            lines.append(f"- {e}")
    if notes:
        lines.append("【提示】")
        for n in notes:
            lines.append(f"- {n}")
    if leaderboard:
        lines.append("【全场排名】(格式:P 车手 轮胎 该车落后领先者的秒数)")
        for row in _trim_leaderboard(leaderboard):
            if row is None:
                lines.append("...")
                continue
            mark = "*" if row.get("is_player") else " "
            gap = (row.get("gap_to_leader_ms") or 0) / 1000.0
            gap_s = "领先全场" if row["position"] == 1 else f"落后 {gap:.1f}s"
            lines.append(
                f"{row['position']}.{mark}{row['driver']} {row.get('tyre') or '?'} {gap_s}")
    return "\n".join(lines)


def _trim_leaderboard(leaderboard: list, top: int = 3) -> list:
    """Top N + player + immediate neighbours; None rows mark elisions."""
    out = []
    player_idx = next((i for i, r in enumerate(leaderboard) if r.get("is_player")), None)
    keep = set(range(min(top, len(leaderboard))))
    if player_idx is not None:
        for d in (-1, 0, 1):
            j = player_idx + d
            if 0 <= j < len(leaderboard):
                keep.add(j)
    prev = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append(None)
        out.append(leaderboard[i])
        prev = i
    return out


def _select_facts(question: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    """Return the fact subset relevant to the question's intent (T9).

    Namespaced prefixes: tyre.*/stint.*/pace.*, gap.*/position.*,
    pit.*/fuel.*. Matching keeps only those namespaces plus the always-useful
    core (lap/position/compound). No match -> full facts (safe default).
    """
    q = (question or "").lower()
    groups = []
    # Flat (un-namespaced) fact prefixes to keep for the matched intents.
    flat = []
    core = ("lap", "total_laps", "position", "last_lap_time", "best_lap_time",
            "current_lap_time", "tyre_compound", "tyre_age_laps", "speed_kph")
    if any(k in q for k in ("胎", "轮胎", "磨损", "胎温", "stint", "衰退")):
        groups.append(("tyre", "stint", "pace"))
        flat.append("tyre_")
    if any(k in q for k in ("前车", "后面", "落后", "差距", "追", "超", "名次", "位置", "gap")):
        groups.append(("gap", "position"))
    if any(k in q for k in ("进站", "策略", "油", "窗口", "pit", "fuel", "plan")):
        groups.append(("pit", "fuel", "stint"))
        # Flat fuel facts (fuel_kg, fuel_laps_left, fuel_surplus_laps ...) carry
        # no namespace prefix; keep them so the model can actually answer fuel
        # questions instead of seeing only the namespaced ones.
        flat.append("fuel_")
    if any(k in q for k in ("天气", "雨", "weather", "rain")):
        groups.append(("weather",))
    if any(k in q for k in ("排位", "最快圈", "杆位", "quali")):
        groups.append(("qualifying",))
    prefixes = tuple(f"{g}." for grp in groups for g in grp)
    if not prefixes:
        return facts
    out = {}
    for k, v in facts.items():
        if k in core or k.startswith(prefixes) or k.startswith(tuple(flat)):
            out[k] = v
    return out or facts


def build_messages(question: str, summary: Dict[str, Any],
                   history: list | None = None, profile: Any = None) -> list:
    """Assemble the message list for the chat API.

    Args:
        question: the driver's question text.
        summary: output of Summariser.summarise().
        history: optional short list of prior {role, content} turns.
        profile: optional profiles.Profile controlling style and history depth.
    """
    facts = summary.get("facts", {})
    notes = summary.get("notes", [])
    leaderboard = summary.get("leaderboard")
    recent_events = summary.get("recent_events")
    # T9: pick only the fact namespaces relevant to the question (cheaper,
    # sharper). Unmatched intents fall back to the full facts.
    facts = _select_facts(question, facts)
    snapshot = build_snapshot_text(facts, notes, leaderboard, recent_events)

    system = SYSTEM_PROMPT
    max_history = 4
    if profile is not None:
        if getattr(profile, "style", ""):
            system = f"{SYSTEM_PROMPT}\n\n{profile.style}"
        max_history = getattr(profile, "max_history", max_history)

    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-max_history:])
    messages.append({"role": "user", "content": f"{snapshot}\n\n车手提问: {question}"})
    return messages
