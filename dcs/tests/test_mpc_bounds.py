"""The MPC must respect level bounds even on a large setpoint change, and
PID.SP must never leave the ported model's valid envelope
(MODEL_LEVEL_MIN_M..MODEL_LEVEL_MAX_M, see controller_wrapper.py), which is
the practical bound the DCS enforces regardless of what target is asked
for.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dcs.controller_wrapper import MODEL_LEVEL_MAX_M, MODEL_LEVEL_MIN_M, UnitController


def test_large_setpoint_change_stays_within_bounds():
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    level_m = 0.02
    # Ask for a large jump, close to the top of the model's valid range.
    target_m = MODEL_LEVEL_MAX_M * 0.95

    for _ in range(60):
        result = controller.step(target_m, level_m, level_hh_m=9.0, level_ll_m=0.5)
        assert MODEL_LEVEL_MIN_M - 1e-9 <= result.sp_m <= MODEL_LEVEL_MAX_M + 1e-9, (
            f"PID.SP {result.sp_m:.4f} m left the model's valid envelope "
            f"[{MODEL_LEVEL_MIN_M}, {MODEL_LEVEL_MAX_M}]"
        )
        level_m = result.sp_m  # pretend the PID tracks the SP exactly

    # After enough cycles the setpoint trajectory should have made real
    # progress toward the target, not stayed pinned at the start.
    assert level_m > 0.05


def test_target_above_model_ceiling_does_not_overshoot_bound():
    """Ask for a target above what the model can physically represent
    (bigger than H2_MAX). PID.SP must still clip to the model ceiling
    instead of being written past it.
    """
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    level_m = 0.02
    target_m = 5.0  # far outside the ported rig model's range

    for _ in range(60):
        result = controller.step(target_m, level_m, level_hh_m=9.0, level_ll_m=0.5)
        assert result.sp_m <= MODEL_LEVEL_MAX_M + 1e-9
        level_m = result.sp_m


def test_solver_failure_holds_last_setpoint():
    """When the underlying MPC step reports non-convergence, the wrapper
    must return the last good SP unchanged, not a fresh (unvalidated) value.
    """
    controller = UnitController(unit_id=1)
    controller.request_bumpless_reset()

    result = controller.step(0.15, 0.05, level_hh_m=9.0, level_ll_m=0.5)
    assert result.converged
    good_sp = result.sp_m

    # Force a failure the same way controller.controller.step() would
    # report one: monkeypatch the inner controller's step to simulate
    # non-convergence, as if ipopt failed on this cycle.
    from reference.water_mpc.mpc_core import MPCResult

    def failing_step(setpoint_m, level_measured_m):
        return MPCResult(flow_cm3s=0.0, solve_time_ms=999.0, converged=False)

    controller.controller.step = failing_step
    held = controller.step(0.15, level_measured_m=good_sp, level_hh_m=9.0, level_ll_m=0.5)

    assert held.converged is False
    assert held.held_last_sp is True
    assert held.sp_m == good_sp
