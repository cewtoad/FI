"""AI profiles and the zero-cost local fast-answer router.

A *profile* is a named (max_tokens, prompt style, history depth) combination.
It only ever changes the expression layer - the numbers always come from the
summariser, never from the model.

The *local router* answers a set of high-frequency questions straight from the
summariser facts, with no LLM call at all. That keeps the voice path's worst
latency and token cost down, and is deterministic by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class Profile:
    name: str
    max_tokens: int
    max_history: int
    style: str  # extra instruction appended to the system prompt
    local_first: bool = True


PROFILES: Dict[str, Profile] = {
    "fast": Profile(
        name="fast", max_tokens=200, max_history=2,
        style="【快速档】只回一句话,禁止展开,禁止任何补充提醒。"),
    "standard": Profile(
        name="standard", max_tokens=400, max_history=6, style=""),
    "deep": Profile(
        name="deep", max_tokens=800, max_history=10,
        style=("【策略档】可给 2-3 句,主动给出策略权衡"
               "(进站窗口/轮胎寿命/油耗),但仍然不要报流水账。"),
    ),
}

DEFAULT_PROFILE = "standard"


def get_profile(name: Optional[str]) -> Profile:
    return PROFILES.get((name or "").strip().lower(), PROFILES[DEFAULT_PROFILE])


# --------------------------------------------------------------- local router

@dataclass
class FastAnswer:
    text: str
    intent: str


def _fmt_fact(facts: Dict[str, Any], key: str) -> Optional[str]:
    v = facts.get(key)
    if v is None or v == "" or v == "-":
        return None
    return str(v)


def _fmt_ms(ms: int) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return "-"
    m, rem = divmod(ms, 60000)
    s, milli = divmod(rem, 1000)
    return f"{m}:{s:02d}.{milli:03d}" if m else f"{s}.{milli:03d}"


def _rival_answer(name: str, row: dict) -> Optional[FastAnswer]:
    lap = row.get("last_lap_ms")
    if isinstance(lap, int) and lap > 0:
        return FastAnswer(text=f"{name} 上一圈 {_fmt_ms(lap)}", intent="rival_pace")
    return None


def _position(facts: Dict[str, Any]) -> Optional[str]:
    t = _fmt_fact(facts, "position")
    return f"你 P{t}" if t else None


def _route_position(facts, q):
    return _position(facts)


def _route_lap(facts, q):
    lap = _fmt_fact(facts, "lap")
    if not lap:
        return None
    return f"第 {lap} 圈"


def _route_laps_remaining(facts, q):
    """Answer "how many laps left" (distinct from "which lap am I on")."""
    remaining = facts.get("laps_remaining")
    if isinstance(remaining, int):
        if remaining <= 0:
            return "比赛已到最后阶段"
        return f"还剩 {remaining} 圈(含本圈)"
    # Quali / practice have no lap count: report the session clock instead.
    left = facts.get("session_time_left_s")
    if isinstance(left, (int, float)) and left > 0:
        return f"本阶段还剩 {int(left) // 60} 分 {int(left) % 60} 秒"
    return None


def _route_last_lap(facts, q):
    t = _fmt_fact(facts, "last_lap_time")
    return f"上一圈 {t}" if t else None


def _route_best_lap(facts, q):
    t = _fmt_fact(facts, "best_lap_time")
    return f"最快圈 {t}" if t else None


def _route_gap_ahead(facts, q):
    g = _fmt_fact(facts, "gap_to_front")
    if not g:
        # T1.2: no front-gap data (e.g. leader) must not be reported as
        # "你在领跑" - only an explicit "领跑" fact says that.
        return None
    if g == "领跑":
        return "你在领跑"
    return f"距前车 {g}"


def _route_gap_leader(facts, q):
    g = _fmt_fact(facts, "gap_to_leader")
    if not g:
        return None
    return g if g == "领先全场" else f"距领先者 {g}"


def _route_fuel(facts, q):
    surplus = facts.get("fuel_surplus_laps")
    if isinstance(surplus, (int, float)):
        if surplus < -0.2:
            return f"油量不足,完赛缺 {abs(surplus):.2f} 圈"
        if surplus > 1.0:
            return f"油量富余 {surplus:.2f} 圈"
        return "油量刚好够完成比赛"
    v = _fmt_fact(facts, "fuel_laps_left")
    return f"剩余油量可跑 {v} 圈" if v else None


def _route_tyre(facts, q):
    c = _fmt_fact(facts, "tyre_compound")
    a = _fmt_fact(facts, "tyre_age_laps")
    if not c:
        return None
    return f"当前 {c},{a} 圈胎龄" if a else f"当前 {c}"


def _route_tyre_temp(facts, q):
    t = facts.get("tyre_temp_c")
    if isinstance(t, list) and t:
        return "胎温 " + "/".join(str(x) for x in t) + "C"
    return None


def _route_damage(facts, q):
    out = []
    if facts.get("engine_critical"):
        out.append("引擎严重受损")
    if facts.get("drs_fault"):
        out.append("DRS 故障")
    if facts.get("ers_fault"):
        out.append("ERS 故障")
    if facts.get("damage_bodywork_max_pct") is not None:
        out.append(f"车损最高 {facts['damage_bodywork_max_pct']}%")
    return ",".join(out) if out else ("车辆无显著损伤" if "damage_bodywork_max_pct" in facts else None)


def _route_pit(facts, q):
    st = _fmt_fact(facts, "pit_status")
    if st:
        return f"进站状态 {st}"
    return None


def _route_car_ahead(facts, q):
    c = _fmt_fact(facts, "car_ahead")
    return f"前车 {c}" if c else None


def _route_tyre_life(facts, q):
    life = facts.get("stint.tyre_laps_to_limit")
    if isinstance(life, (int, float)):
        return f"轮胎预计还能跑 {life:.0f} 圈"
    return None


def _route_pit_window(facts, q):
    state = _fmt_fact(facts, "pit.window_state")
    if not state:
        return None
    ideal = _fmt_fact(facts, "pit.ideal_lap")
    latest = _fmt_fact(facts, "pit.latest_lap")
    rejoin = _fmt_fact(facts, "pit.rejoin_position")
    if state == "open":
        return f"进站窗口已开（理想第 {ideal} 圈，最晚第 {latest} 圈）"
    if state == "not_open":
        return f"进站窗口未开（理想第 {ideal} 圈）"
    if state == "last_lap":
        return f"本圈是进站窗口最后一圈（第 {latest} 圈）"
    if state == "missed":
        return f"已错过进站窗口（最晚第 {latest} 圈）"
    if rejoin:
        return f"进站窗口状态 {state}，预计出站第 {rejoin} 位"
    return f"进站窗口状态 {state}"


def _route_weather(facts, q):
    eta = facts.get("weather.rain_eta_min")
    if isinstance(eta, (int, float)):
        return f"预计 {eta:.0f} 分钟后降雨"
    rp = _fmt_fact(facts, "rain_percentage")
    if rp:
        return f"当前降雨概率 {rp}%"
    return None


# Ordered intent table: (regex, handler, label). First match wins.
_ROUTES: List[tuple] = [
    (re.compile(r"前车|前面|前方|追逐"), _route_car_ahead, "car_ahead"),
    (re.compile(r"落后.*领先|距.*领先|距领先|差.*头车|离.*头车"), _route_gap_leader, "gap_leader"),
    (re.compile(r"距.*前车|差.*前车|和前车"), _route_gap_ahead, "gap_ahead"),
    (re.compile(r"最快圈|最好.*圈|best"), _route_best_lap, "best_lap"),
    (re.compile(r"上[一]?圈|上一圈速度"), _route_last_lap, "last_lap"),
    # "还剩几圈" must be tested before the generic lap-count route.
    (re.compile(r"还剩几圈|还剩多少圈|还有几圈|剩几圈|剩余圈数"),
     _route_laps_remaining, "laps_remaining"),
    (re.compile(r"第几圈|圈数"), _route_lap, "lap"),
    (re.compile(r"我.*p几|我.*第几|什么名次|排名|位置"), _route_position, "position"),
    (re.compile(r"油|燃油|油耗"), _route_fuel, "fuel"),
    (re.compile(r"胎还能|轮胎还能|还能跑几圈|轮胎寿命|撑几圈"), _route_tyre_life, "tyre_life"),
    (re.compile(r"进站窗口|什么时候进站|进站时机|window"), _route_pit_window, "pit_window"),
    (re.compile(r"天气|下雨|降雨|雨"), _route_weather, "weather"),
    (re.compile(r"胎温|轮胎温度"), _route_tyre_temp, "tyre_temp"),
    (re.compile(r"轮胎|胎龄|什么胎"), _route_tyre, "tyre"),
    (re.compile(r"损伤|车损|受损|坏了"), _route_damage, "damage"),
    (re.compile(r"进站|维修区|pit"), _route_pit, "pit"),
]


class LocalRouter:
    """Answer deterministic questions straight from summariser facts."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.intent_counts: Dict[str, int] = {}

    def answer(self, question: str, facts: Dict[str, Any],
               leaderboard: Optional[list] = None,
               name_renderer: Any = None) -> Optional[FastAnswer]:
        q = (question or "").strip().lower()
        if not q:
            return None
        # T9: rival pace by name (needs the leaderboard, so handled here).
        rival = self._try_rival_pace(q, leaderboard, name_renderer)
        if rival is not None:
            self.hits += 1
            self.intent_counts["rival_pace"] = self.intent_counts.get("rival_pace", 0) + 1
            return rival
        for pattern, handler, label in _ROUTES:
            if pattern.search(q):
                text = handler(facts, q)
                if text:
                    self.hits += 1
                    self.intent_counts[label] = self.intent_counts.get(label, 0) + 1
                    return FastAnswer(text=text, intent=label)
        self.misses += 1
        return None

    @staticmethod
    def _try_rival_pace(q, leaderboard, name_renderer) -> Optional[FastAnswer]:
        if not leaderboard or name_renderer is None:
            return None
        if not any(k in q for k in ("圈速", "配速", "速度", "多少", "单圈")):
            return None
        # 1) direct match on the leaderboard driver string
        for row in leaderboard:
            name = (row.get("driver") or "")
            if name and name.lower() in q:
                return _rival_answer(name, row)
        # 2) match via the seed table: a Chinese/English alias in the query maps
        #    to a driver code, then find the leaderboard row containing it.
        seeds = getattr(name_renderer, "_drivers", [])
        for d in seeds:
            code = str(d.get("code", ""))
            aliases = [str(d.get("zh", "")), code, code.title()]
            if any(a and a.lower() in q for a in aliases):
                for row in leaderboard:
                    driver = (row.get("driver") or "").upper()
                    if code.upper() in driver:
                        return _rival_answer(row.get("driver") or code, row)
        return None

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "intents": dict(self.intent_counts),
        }
