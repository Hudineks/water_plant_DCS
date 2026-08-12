"""Tests for the two-tank ODE itself, independent of PID/interlocks.

This is the crux of the v2 rework: plc/model.py must behave like a faithful
port of reference/water_mpc/mpc_core.py's physics (same constants), just
driven by a percentage pump command instead of a raw cm3/s input.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plc.model import TankModel, F_AREA, K_OUT

DT = 0.1


def test_zero_input_drains_to_zero():
    model = TankModel(h1_cm=10.0, h2_cm=10.0)
    for _ in range(20000):
        model.step(pump_cmd_pct=0.0, dt_s=DT)
    assert model.h1_cm < 0.01
    assert model.h2_cm < 0.01


def test_levels_never_negative():
    model = TankModel(h1_cm=0.5, h2_cm=0.5)
    for _ in range(50):
        model.step(pump_cmd_pct=0.0, dt_s=DT)
    assert model.h1_cm >= 0.0
    assert model.h2_cm >= 0.0


def test_full_pump_converges_to_torricelli_balance():
    # At steady state with a constant inflow, K_OUT*sqrt(h) = inflow for
    # each tank in series: h1 steady state balances q0 against outflow to
    # h2, and h2 in turn balances that same outflow (in series, in steady
    # state, the flow through both orifices equals q0).
    model = TankModel(h1_cm=0.0, h2_cm=0.0)
    q0 = model.pump_max_flow_cm3s

    for _ in range(200_000):
        model.step(pump_cmd_pct=100.0, dt_s=DT)

    expected_h1 = (q0 / K_OUT) ** 2
    expected_h2 = (q0 / K_OUT) ** 2
    assert math.isclose(model.h1_cm, expected_h1, rel_tol=0.02)
    assert math.isclose(model.h2_cm, expected_h2, rel_tol=0.02)


def test_tank1_leads_tank2_during_fill():
    model = TankModel(h1_cm=0.0, h2_cm=0.0)
    for _ in range(50):
        model.step(pump_cmd_pct=100.0, dt_s=DT)
    assert model.h1_cm > model.h2_cm
