"""Bumpless transfer: when APC.Enabled transitions to true, the first PID.SP
the DCS writes must move along the MPC's own dynamically-consistent plan
computed from the current measurement, not snap to an unrelated value or
straight to the far-away target. This is what reset_to_measurement() is
for: it seeds the EKF/MPC's internal state at the real plant measurement
before the first solve.

"Bumpless" does not mean "stays pinned near the measurement" -- with
PREDICTION_HORIZON_INDEX set deep enough into the solve horizon for the
local PID to have real corrective authority (see
dcs/controller_wrapper.py's comment on that constant, and
OPEN_QUESTIONS.md for the closed-loop instability a too-shallow index
causes), a real first step is expected to already carry meaningful
separation from the measurement when the target is far away. What must
NOT happen is a jump inconsistent with the model's own physics (e.g. to a
value near the target or outside the model's valid envelope) merely
because the estimator was just reset.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dcs.controller_wrapper import UnitController

TARGET_LEVEL_M = 0.15
MAX_FIRST_STEP_JUMP_M = 0.05  # generous bound: real, but not a snap to the target


def test_first_sp_after_enable_is_close_to_measurement():
    controller = UnitController(unit_id=1)
    measured_level_m = 0.05

    controller.request_bumpless_reset()
    result = controller.step(
        target_level_m=TARGET_LEVEL_M,
        level_measured_m=measured_level_m,
        level_hh_m=9.0,
        level_ll_m=0.5,
    )

    assert result.converged
    jump = abs(result.sp_m - measured_level_m)
    assert jump < MAX_FIRST_STEP_JUMP_M, (
        f"first PID.SP after enable jumped {jump:.4f} m from the measurement, "
        f"expected < {MAX_FIRST_STEP_JUMP_M} m for bumpless transfer"
    )


def test_bumpless_reset_reseeds_after_disable_reenable_at_different_level():
    """Simulates: APC runs for a while, gets disabled, the plant drifts to a
    different level under local PID, then APC is re-enabled. The next SP
    must track the new measurement, not whatever the controller's internal
    state was left at.
    """
    controller = UnitController(unit_id=1)

    controller.request_bumpless_reset()
    level_m = 0.05
    for _ in range(20):
        result = controller.step(TARGET_LEVEL_M, level_m, 9.0, 0.5)
        level_m = result.sp_m  # pretend the PID tracks the SP exactly

    # APC disabled here; plant drifts under local control to a new level.
    drifted_level_m = 0.02
    controller.request_bumpless_reset()
    result = controller.step(TARGET_LEVEL_M, drifted_level_m, 9.0, 0.5)

    assert result.converged
    jump = abs(result.sp_m - drifted_level_m)
    assert jump < MAX_FIRST_STEP_JUMP_M, (
        f"first PID.SP after re-enable jumped {jump:.4f} m from the drifted "
        f"measurement, expected < {MAX_FIRST_STEP_JUMP_M} m"
    )
