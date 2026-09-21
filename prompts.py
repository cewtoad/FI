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

禁止:
- 不要输出思考、解释、markdown、列表符号、编号。
- 不要报流水账。只给车手听的那一句。"""


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
        lines.append("【全场排名】(P车手/轮胎/落后领先)")
        for row in _trim_leaderboard(leaderboard):
            if row is None:
                lines.append("...")
                continue
            mark = "*" if row.get("is_player") else " "
            gap = (row.get("gap_to_leader_ms") or 0) / 1000.0
            gap_s = "领先" if row["position"] == 1 else f"+{gap:.1f}s"
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


def build_messages(question: str, summary: Dict[str, Any],
                   history: list | None = None) -> list:
    """Assemble the message list for the chat API.

    Args:
        question: the driver's question text.
        summary: output of Summariser.summarise().
        history: optional short list of prior {role, content} turns.
    """
    facts = summary.get("facts", {})
    notes = summary.get("notes", [])
    leaderboard = summary.get("leaderboard")
    recent_events = summary.get("recent_events")
    snapshot = build_snapshot_text(facts, notes, leaderboard, recent_events)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-4:])  # cap context to keep tokens low
    messages.append({"role": "user", "content": f"{snapshot}\n\n车手提问: {question}"})
    return messages
