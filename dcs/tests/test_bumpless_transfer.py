"""Bumpless transfer: when a unit starts being solved again (re-enabled,
or its EKF/MPC state gets reseeded via reset_to_measurement()), the very
next flow command must be a physically sane response to the *current*
measurement and target, not a discontinuity inherited from whatever the
controller's internal state happened to be before the reset.

PID.SP is a flow (cm3/s), not a level: "bumpless" no longer means "stays
near the current level" (a flow and a level are not the same kind of
quantity, so there is nothing to compare a flow jump against in level
terms). It means the flow command respects the actuator's physical bounds
and points the right direction for the actual gap between measurement and
target -- inflow when below target, near-zero when at or above it (the
model has no active way to drain faster, only gravity outflow).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dcs.controller_wrapper import UnitController
from reference.water_mpc.mpc_core import U_MAX, U_MIN

TARGET_LEVEL_M = 0.15


def test_first_flow_after_reset_is_bounded_and_points_up_when_below_target():
    controller = UnitController(unit_id=1)
    measured_level_m = 0.05  # well below target

    controller.request_bumpless_reset()
    result = controller.step(
        target_level_m=TARGET_LEVEL_M,
        level_measured_m=measured_level_m,
        level_hh_m=9.0,
        level_ll_m=0.5,
    )

    assert result.converged
    assert U_MIN - 1e-9 <= result.sp_flow_cm3s <= U_MAX + 1e-9
    # Below target: the model has no active drain, so making progress
    # requires some positive inflow.
    assert result.sp_flow_cm3s > 0.0


def test_reset_reseeds_after_disable_reenable_at_a_different_level():
    """Simulates: APC runs for a while, gets disabled, the plant drifts to
    a different level under local control, then APC is re-enabled. The
    next flow command must be a sane response to the *new* measurement,
    not whatever the controller's internal state was left at.
    """
    controller = UnitController(unit_id=1)

    controller.request_bumpless_reset()
    level_m = 0.05
    for _ in range(20):
        controller.step(TARGET_LEVEL_M, level_m, 9.0, 0.5)
        # A real plant does not track flow instantaneously; nudge the
        # simulated level slightly toward target so there is somewhere
        # for the loop below to reset away from.
        level_m = min(TARGET_LEVEL_M, level_m + 0.002)

    # APC disabled here; plant drifts under local control to a level far
    # below both the target and wherever the loop above left off.
    drifted_level_m = 0.02
    controller.request_bumpless_reset()
    result = controller.step(TARGET_LEVEL_M, drifted_level_m, 9.0, 0.5)

    assert result.converged
    assert U_MIN - 1e-9 <= result.sp_flow_cm3s <= U_MAX + 1e-9
    assert result.sp_flow_cm3s > 0.0  # still below target, still needs inflow
