"""Prompts for the F1 race engineer Q&A.

The design goal is to answer driver radio-style questions cheaply and
consistently. Two things keep token cost low:

  1. We never send raw telemetry. The state summariser already compressed it to
     a small ``facts`` dict + a short note list. The prompt embeds that compact
     snapshot only.
  2. The system prompt pins the persona, language, length and the rule that the
     model must only use the provided numbers (no inventing laps or teams).

The model is an F1-TV-style race engineer answering the driver's question.

Customisation (P1): users may add an overlay via ``CUSTOM_SYSTEM_PROMPT``
(one line in .env) and/or ``custom_system_prompt.txt`` next to .env. The
hard ``SAFETY_LINE`` is always appended last and cannot be removed.
"""

from __future__ import annotations

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


# Always appended last. Not overridable by CUSTOM_SYSTEM_PROMPT or the
# custom_system_prompt.txt file — keeps "advise only, never press keys".
SAFETY_LINE = (
    "【硬规则·不可覆盖】只给出情报与建议，不得指示、模拟或代为按下"
    "键盘/手柄/方向盘按键；本程序不会发出任何游戏输入。"
)

CUSTOM_PROMPT_FILENAME = "custom_system_prompt.txt"


def _read_custom_prompt_file() -> str:
    """Optional multi-line overlay next to .env (app_root). Missing = empty."""
    try:
        from paths import app_root
        path = app_root() / CUSTOM_PROMPT_FILENAME
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_custom_prompt_file(text: str) -> str:
    """Persist multi-line overlay; empty text deletes the file. Returns path."""
    from paths import app_root
    path = app_root() / CUSTOM_PROMPT_FILENAME
    cleaned = (text or "").strip()
    if not cleaned:
        if path.is_file():
            path.unlink()
        return str(path)
    # Cap size so a paste accident cannot bloat prompts forever.
    if len(cleaned) > 8000:
        raise ValueError("custom prompt too long (max 8000 chars)")
    path.write_text(cleaned + "\n", encoding="utf-8")
    return str(path)


def load_custom_overlay(config: Any = None) -> str:
    """User overlay = file (if any) + optional one-line CUSTOM_SYSTEM_PROMPT.

    Neither can remove SAFETY_LINE.
    """
    parts = []
    file_text = _read_custom_prompt_file()
    if file_text:
        parts.append(file_text)
    try:
        if config is None:
            from config import get_config
            config = get_config()
        one = (config.get("CUSTOM_SYSTEM_PROMPT", "") or "").strip()
        if one:
            parts.append(one)
    except Exception:
        pass
    return "\n\n".join(parts).strip()


def effective_system_prompt(profile: Any = None, config: Any = None) -> str:
    """Built-in SYSTEM_PROMPT + profile style + user overlay + SAFETY_LINE."""
    system = SYSTEM_PROMPT
    if profile is not None and getattr(profile, "style", ""):
        system = f"{SYSTEM_PROMPT}\n\n{profile.style}"
    overlay = load_custom_overlay(config)
    if overlay:
        system = f"{system}\n\n【用户自定义补充】\n{overlay}"
    return f"{system}\n\n{SAFETY_LINE}"


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
                   history: list | None = None, profile: Any = None,
                   config: Any = None) -> list:
    """Assemble the message list for the chat API.

    Args:
        question: the driver's question text.
        summary: output of Summariser.summarise().
        history: optional short list of prior {role, content} turns.
        profile: optional profiles.Profile controlling style and history depth.
        config: optional config for CUSTOM_SYSTEM_PROMPT (defaults to get_config).
    """
    facts = summary.get("facts", {})
    notes = summary.get("notes", [])
    leaderboard = summary.get("leaderboard")
    recent_events = summary.get("recent_events")
    # T9: pick only the fact namespaces relevant to the question (cheaper,
    # sharper). Unmatched intents fall back to the full facts.
    facts = _select_facts(question, facts)
    snapshot = build_snapshot_text(facts, notes, leaderboard, recent_events)

    max_history = 4
    if profile is not None:
        max_history = getattr(profile, "max_history", max_history)
    system = effective_system_prompt(profile, config)

    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-max_history:])
    messages.append({"role": "user", "content": f"{snapshot}\n\n车手提问: {question}"})
    return messages
