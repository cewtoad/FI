"""T1.3: tyre overheating must be judged on the ~3s inner-temperature median,
so a short brake-zone spike does not raise a false alarm.
"""

from __future__ import annotations

from state import TelemetryState
from summariser import Summariser


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _car_temp_snapshot(state: TelemetryState) -> dict:
    """Build a snapshot with the car's inner-temp median attached."""
    latest = {"car": {"tyres_inner_temp_median_c": state.tyre_inner_temp_median_c()}}
    return {"latest": latest, "session": {}, "delta": {}, "fuel": {},
            "trends": {}, "leaderboard": [], "events": []}


def _push(state: TelemetryState, temps: list) -> None:
    # Feed straight into the internal sampler (mirrors _on_car_telemetry).
    state._tyre_inner_samples.append((state._clock(), temps))
    cutoff = state._clock() - state.TYRE_TEMP_MEDIAN_WINDOW_S
    while state._tyre_inner_samples and state._tyre_inner_samples[0][0] < cutoff:
        state._tyre_inner_samples.popleft()


def test_brake_spike_does_not_trigger_overheat():
    clk = _Clock()
    st = TelemetryState(clock=clk)
    # 2s of normal running (90C) with a single-frame 130C spike.
    for _ in range(20):
        _push(st, [90, 90, 90, 90])
        clk.advance(0.1)
    _push(st, [130, 130, 130, 130])  # one-frame brake-zone spike
    clk.advance(0.05)
    _push(st, [90, 90, 90, 90])

    snap = _car_temp_snapshot(st)
    summary = Summariser(config=None).summarise(snap)
    assert not any("胎温过高" in n for n in summary["notes"]), summary["notes"]


def test_sustained_hot_triggers_overheat():
    clk = _Clock()
    st = TelemetryState(clock=clk)
    for _ in range(30):
        _push(st, [115, 118, 112, 116])
        clk.advance(0.1)
    snap = _car_temp_snapshot(st)
    summary = Summariser(config=None).summarise(snap)
    assert any("胎温过高" in n for n in summary["notes"]), summary["notes"]


def test_threshold_is_configurable():
    clk = _Clock()
    st = TelemetryState(clock=clk)
    for _ in range(30):
        _push(st, [100, 100, 100, 100])
        clk.advance(0.1)
    snap = _car_temp_snapshot(st)

    class Cfg:
        def get_float(self, key, default):
            return 95.0  # lower the threshold -> hot now

    summary = Summariser(config=Cfg()).summarise(snap)
    assert any("胎温过高" in n for n in summary["notes"]), summary["notes"]
