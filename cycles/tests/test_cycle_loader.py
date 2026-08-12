"""Round-trip tests for SetpointCycle against the two real demo CSVs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cycles.loader import SetpointCycle

CYCLES_DIR = Path(__file__).resolve().parent.parent


def test_step_response_parses():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    assert cycle.period_s == 4.0 * 60.0
    assert len(cycle.rows) == 6
    assert cycle.rows[0] == (0.0, 5.0)
    assert cycle.rows[-1] == (3.99 * 60.0, 5.0)


def test_ramp_response_parses():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "ramp_response.csv")
    assert cycle.period_s == 4.0 * 60.0
    assert len(cycle.rows) == 3


def test_step_value_at_holds_between_steps():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    # Flat at 5.0 cm from t=0 to just before the step at t=2.0min.
    assert cycle.value_at(0.0) == 5.0
    assert cycle.value_at(60.0) == 5.0
    # Flat at 10.0 cm from t=2.01min to t=3.0min.
    assert cycle.value_at(2.5 * 60.0) == 10.0


def test_ramp_value_at_interpolates_linearly():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "ramp_response.csv")
    # Ramp rows: (0min,0cm) (2min,15cm) (4min,0cm) -- midpoint of the
    # 0min -> 2min segment should be the linear midpoint.
    value = cycle.value_at(60.0)
    assert abs(value - 7.5) < 0.1


def test_value_at_wraps_on_period():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    period = cycle.period_s
    # One full period later should repeat the same value.
    assert cycle.value_at(60.0) == cycle.value_at(60.0 + period)


def test_value_at_respects_t_start_offset():
    cycle = SetpointCycle.from_csv(CYCLES_DIR / "step_response.csv")
    # Anchoring the cycle's start at t_start=100 should shift the phase.
    assert cycle.value_at(100.0, t_start_s=100.0) == cycle.value_at(0.0, t_start_s=0.0)
