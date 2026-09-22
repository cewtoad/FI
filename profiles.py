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


def _route_last_lap(facts, q):
    t = _fmt_fact(facts, "last_lap_time")
    return f"上一圈 {t}" if t else None


def _route_best_lap(facts, q):
    t = _fmt_fact(facts, "best_lap_time")
    return f"最快圈 {t}" if t else None


def _route_gap_ahead(facts, q):
    g = _fmt_fact(facts, "gap_to_front")
    if not g or g == "领跑":
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


# Ordered intent table: (regex, handler, label). First match wins.
_ROUTES: List[tuple] = [
    (re.compile(r"前车|前面|前方|追逐"), _route_car_ahead, "car_ahead"),
    (re.compile(r"落后.*领先|距.*领先|距领先|差.*头车|离.*头车"), _route_gap_leader, "gap_leader"),
    (re.compile(r"距.*前车|差.*前车|和前车"), _route_gap_ahead, "gap_ahead"),
    (re.compile(r"最快圈|最好.*圈|best"), _route_best_lap, "best_lap"),
    (re.compile(r"上[一]?圈|上一圈速度"), _route_last_lap, "last_lap"),
    (re.compile(r"第几圈|还剩几圈|还剩多少圈|圈数"), _route_lap, "lap"),
    (re.compile(r"我.*p几|我.*第几|什么名次|排名|位置"), _route_position, "position"),
    (re.compile(r"油|燃油|油耗"), _route_fuel, "fuel"),
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

    def answer(self, question: str, facts: Dict[str, Any]) -> Optional[FastAnswer]:
        q = (question or "").strip().lower()
        if not q:
            return None
        for pattern, handler, label in _ROUTES:
            if pattern.search(q):
                text = handler(facts, q)
                if text:
                    self.hits += 1
                    self.intent_counts[label] = self.intent_counts.get(label, 0) + 1
                    return FastAnswer(text=text, intent=label)
        self.misses += 1
        return None

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "intents": dict(self.intent_counts),
        }
