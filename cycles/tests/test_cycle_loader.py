"""Round-trip tests for SetpointCycle against the two real demo CSVs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cycles.loader import SetpointCycle

CYCLES_DIR = Path(__file__).resolve().parent.parent


def test_step_response_parses():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    assert cycle.period_s == 10.0 * 60.0
    assert len(cycle.rows) == 5
    assert cycle.rows[0] == (0.0, 8.0)
    assert cycle.rows[-1] == (6.66 * 60.0, 8.0)


def test_ramp_response_parses():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "ramp_response.csv")
    assert cycle.period_s == 13.0 * 60.0
    assert len(cycle.rows) == 4


def test_step_value_at_holds_between_steps():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    # Flat at 8.0 cm from t=0 to just before the step at t=3.32min.
    assert cycle.value_at(0.0) == 8.0
    assert cycle.value_at(60.0) == 8.0
    # Flat at 14.0 cm from t=3.33min to t=6.65min.
    assert cycle.value_at(4.0 * 60.0) == 14.0


def test_ramp_value_at_interpolates_linearly():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "ramp_response.csv")
    # Ramp rows: (0,8) (3.33min,8) (6.66min,14) (10min,8) -- midpoint of the
    # 3.33min -> 6.66min segment should be the linear midpoint.
    t_start = 3.33 * 60.0
    t_end = 6.66 * 60.0
    t_mid = (t_start + t_end) / 2.0
    value = cycle.value_at(t_mid)
    assert abs(value - 11.0) < 0.1


def test_value_at_wraps_on_period():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    period = cycle.period_s
    # One full period later should repeat the same value.
    assert cycle.value_at(60.0) == cycle.value_at(60.0 + period)


def test_value_at_respects_t_start_offset():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    # Anchoring the cycle's start at t_start=100 should shift the phase.
    assert cycle.value_at(100.0, t_start_s=100.0) == cycle.value_at(0.0, t_start_s=0.0)
