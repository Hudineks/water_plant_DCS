"""The MPC's commanded flow (PID.SP) must never leave the actuator's own
physical bounds (U_MIN..U_MAX, reference/water_mpc/mpc_core.py), regardless
of what level target is asked for, even when that target is unreasonable
or the level is nowhere near it yet.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dcs.controller_wrapper import UnitController
from plc.model import TankModel
from reference.water_mpc.mpc_core import U_MAX, U_MIN


def test_large_setpoint_change_stays_within_bounds():
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    model = TankModel(h1_cm=2.0, h2_cm=2.0)
    target_m = 0.15  # near the model's own ceiling (H2_MAX=20cm=0.2m)

    for _ in range(300):
        level_m = model.h2_cm / 100.0
        result = controller.step(target_m, level_m, level_hh_m=9.0, level_ll_m=0.5)
        assert U_MIN - 1e-9 <= result.sp_flow_cm3s <= U_MAX + 1e-9, (
            f"PID.SP {result.sp_flow_cm3s:.4f} cm3/s left the actuator's valid range "
            f"[{U_MIN}, {U_MAX}]"
        )
        pump_cmd_pct = 100.0 * result.sp_flow_cm3s / model.pump_max_flow_cm3s
        model.step(pump_cmd_pct, dt_s=1.0)

    # After enough cycles the real (simulated) level should have made
    # progress toward the target, not stayed pinned at the start.
    assert model.h2_cm / 100.0 > 0.05


def test_target_above_model_ceiling_does_not_overshoot_flow_bounds():
    """Ask for a level target above what the model can physically
    represent (far beyond H2_MAX). The commanded flow must still respect
    the actuator's own bounds -- the MPC's hard constraint on q0
    (mpc.bounds['upper','_u','q0']=U_MAX in mpc_core.py's build_mpc())
    should make this true regardless of how unreasonable the target is.
    """
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    model = TankModel(h1_cm=2.0, h2_cm=2.0)
    target_m = 5.0  # far outside the ported rig model's range

    for _ in range(60):
        level_m = model.h2_cm / 100.0
        result = controller.step(target_m, level_m, level_hh_m=9.0, level_ll_m=0.5)
        assert U_MIN - 1e-9 <= result.sp_flow_cm3s <= U_MAX + 1e-9
        pump_cmd_pct = 100.0 * result.sp_flow_cm3s / model.pump_max_flow_cm3s
        model.step(pump_cmd_pct, dt_s=1.0)


def test_solver_failure_holds_last_setpoint():
    """When the underlying MPC step reports non-convergence, the wrapper
    must return the last good flow unchanged, not a fresh (unvalidated)
    value.
    """
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    result = controller.step(0.15, 0.05, level_hh_m=9.0, level_ll_m=0.5)
    assert result.converged
    good_flow = result.sp_flow_cm3s

    # Force a failure the same way controller.controller.step() would
    # report one: monkeypatch the inner controller's step to simulate
    # non-convergence, as if ipopt failed on this cycle.
    from reference.water_mpc.mpc_core import MPCResult

    def failing_step(setpoint_m, level_measured_m):
        return MPCResult(flow_cm3s=0.0, solve_time_ms=999.0, converged=False)

    controller.controller.step = failing_step
    held = controller.step(0.15, level_measured_m=0.05, level_hh_m=9.0, level_ll_m=0.5)

    assert held.converged is False
    assert held.held_last_sp is True
    assert held.sp_flow_cm3s == good_flow
