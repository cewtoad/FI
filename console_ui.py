"""Terminal UI: live-refreshing panel showing the summarised telemetry.

Deliberately dependency-free (no curses, no rich). Clears the screen and
redraws a fixed layout on an interval, so it works in any Windows terminal.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

CLEAR = "\x1b[2J\x1b[H"       # clear + home
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _clear() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write(CLEAR)
        sys.stdout.flush()


def _row(label: str, value: Any) -> str:
    return f"  {label:<18}{value}"


class ConsoleUI:
    """Renders summariser output to the terminal."""

    def __init__(self, mode: str = "timetrial") -> None:
        self.mode = mode
        self.status_line = ""

    def render(self, summary: Dict[str, Any], stats: Dict[str, Any],
               connected: bool) -> None:
        _clear()
        facts = summary.get("facts", {})
        notes: List[str] = summary.get("notes", [])
        history: List[str] = summary.get("lap_history", [])

        head = f"F1 TR  [mode: {self.mode}]"
        conn = f"{GREEN}● LIVE{RESET}" if connected else f"{YELLOW}○ waiting for data{RESET}"
        print(f"{CYAN}{head}{RESET}   {conn}")
        print("=" * 52)

        print(f"{CYAN}[ 计时 / Pace ]{RESET}")
        print(_row("Lap", f"{facts.get('lap') or '-'}  P{facts.get('position') or '-'}"))
        print(_row("当前圈", facts.get("current_lap_time")))
        print(_row("上一圈", facts.get("last_lap_time")))
        print(_row("最快圈", facts.get("best_lap_time")))
        print(_row("vs 最快圈", self._color_delta(facts.get("delta_to_best"))))
        print(_row("S1/S2/S3", f"{facts.get('sector1')} / {facts.get('sector2')} / {facts.get('sector3')}"))
        print(_row("距前车", facts.get("gap_to_front")))
        print(_row("距领先", facts.get("gap_to_leader")))
        print()

        print(f"{CYAN}[ 车辆 / Car ]{RESET}")
        print(_row("速度/档位", f"{facts.get('speed_kph')} km/h  档 {facts.get('gear')}"))
        print(_row("轮胎", f"{facts.get('tyre_compound')}  用了 {facts.get('tyre_age_laps')} 圈"))
        print(_row("胎温(4轮)", facts.get("tyre_temp_c")))
        print()

        print(f"{CYAN}[ 燃油 / Fuel ]{RESET}")
        print(_row("剩余", f"{self._round(facts.get('fuel_kg'))} kg  约 {self._round(facts.get('fuel_laps_left'))} 圈"))
        print(_row("消耗率", f"{self._round(facts.get('fuel_rate_kg_per_lap'))} kg/lap"))
        print(_row("剩余圈数", self._round(facts.get("fuel_surplus_laps"))))
        print(_row("预测完赛油", f"{self._round(facts.get('predicted_final_fuel_kg'))} kg"))
        print()

        print(f"{CYAN}[ 圈速历史 ]{RESET}")
        print("  " + "  ".join(history[-8:]) if history else "  (暂无)")
        print()

        print(f"{CYAN}[ 提示 / Notes ]{RESET}")
        if notes:
            for n in notes:
                print(f"  {YELLOW}•{RESET} {n}")
        else:
            print(f"  {DIM}(暂无){RESET}")
        print()

        print(f"{DIM}[ 丢包 ] accepted={stats.get('accepted',0)} "
              f"unparsed={stats.get('dropped_unparsed',0)} "
              f"gate={stats.get('dropped_gate',0)}{RESET}")
        if stats.get("drop_reasons"):
            print(f"{DIM}       reasons={stats['drop_reasons']}{RESET}")

    # ------------------------------------------------------------------ util

    @staticmethod
    def _round(v: Any, nd: int = 2) -> Any:
        if isinstance(v, float):
            return round(v, nd)
        return v

    @staticmethod
    def _color_delta(s: Any) -> str:
        if not isinstance(s, str) or not s.endswith("ms"):
            return str(s)
        try:
            val = int(s.replace("ms", "").replace("+", ""))
        except ValueError:
            return s
        if val > 0:
            return f"{RED}{s}{RESET}"
        if val < 0:
            return f"{GREEN}{s}{RESET}"
        return s
