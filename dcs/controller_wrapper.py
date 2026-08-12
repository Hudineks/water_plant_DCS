"""Wraps reference.water_mpc.WaterTankController for one unit and derives
the PID.SP value that gets written to the PLC.

Mapping from MPC internals to PID.SP (documented in dcs/README.md):

The reference controller's public output (MPCResult.flow_cm3s) is an
actuator flow command. Per the cascade rule (MPC -> PID.SP -> PID -> pump)
this project never writes actuator commands from the supervisory layer, so
flow_cm3s is not used at all here.

Instead, after every controller.step() call, do-mpc has already stored the
predicted state trajectory for the horizon it just solved
(controller.mpc.data.prediction(('_x', 'h2'))). Index 1 of that array is the
model's prediction of the controlled level one t_step (1 s, matching our
cycle time) into the future under the optimal input sequence it just found.
That single point is converted from cm to m and written as the next
PID.SP. This gives the PLC's local PID loop a setpoint trajectory shaped by
the MPC's horizon (it moves the SP gradually toward the operator target
along the path the MPC computed) instead of slamming the full target in one
step, while still never writing to Pump.CMD/Valve.CMD directly.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reference.water_mpc.mpc_core import (  # noqa: E402
    H2_MAX,
    MPCResult,
    TIME_STEP_S,
    WaterTankController,
    build_ekf,
    build_mpc,
    build_model,
)

# The ported model's physically valid envelope for the controlled level
# (h2, cm -> m). See OPEN_QUESTIONS.md: this is much smaller than the plant
# tags' scale (Level.PV random-walks 0-4 m in tools/fake_plc.py, HH default
# 9 m). PID.SP is always clipped to this range regardless of the unit's own
# Level.LL/Level.HH, since those interlock thresholds are set for the real
# tank geometry, not the cm-scale rig the reference controller was tuned
# for, and would clip every SP to Level.LL otherwise.
MODEL_LEVEL_MAX_M = H2_MAX / 100.0
MODEL_LEVEL_MIN_M = 0.0

# Index into the MPC's predicted horizon used as the next PID.SP, out of
# n_horizon=40 (see reference/water_mpc/mpc_core.py). NOT 1 ("one t_step
# ahead"): that value produces a real closed-loop instability, see
# OPEN_QUESTIONS.md. With the two-tank model's real (fast, cm-scale)
# dynamics, the point one second into the MPC's own plan is barely
# different from the current measurement, since the plan reaches its
# target gradually over the full horizon. The local PID then sees a
# near-zero error and applies near-zero pump command, even though the
# MPC's own internal plan calls for a large, sustained flow, so the real
# plant just drains under gravity while the DCS's derived setpoint drifts
# down with it. 25 was chosen empirically (see OPEN_QUESTIONS.md for the
# sweep) as a point far enough into the horizon to carry real, trackable
# separation from the current measurement, while still comfortably inside
# the 40-step horizon rather than at its less-certain terminal edge.
PREDICTION_HORIZON_INDEX = 25


class _PortedWaterTankController(WaterTankController):
    """WaterTankController with its __init__ adapted for do-mpc 5.1.1
    (the version pinned in requirements.txt). The original __init__ from
    reference/water_mpc/mpc_core.py (frozen, not edited) has two problems
    against this do-mpc version, both fixed here without touching the
    model, objective, constraints or solver settings built by
    build_model()/build_mpc()/build_ekf():

    1. mpc.set_tvp_fun() validates its argument by calling it immediately.
       The tvp function reads self._sp_cm, which the original __init__ only
       assigns *after* calling set_tvp_fun, raising AttributeError. Fixed
       by assigning self._sp_cm = 0.0 before set_tvp_fun().
    2. mpc.data.prediction() (used below to read the predicted h2
       trajectory for PID.SP, see this module's docstring) requires
       store_full_solution=True, which the original build_mpc() does not
       set. Fixed by setting it on the mpc object before mpc.setup().

    reset_to_measurement() and step() are inherited unchanged.
    """

    def __init__(self, n_horizon: int = 40, t_step: float = TIME_STEP_S):
        self.t_step = t_step
        self.model = build_model()
        self.mpc = build_mpc(self.model, n_horizon=n_horizon, t_step=t_step)
        self.mpc.settings.store_full_solution = True
        self.ekf = build_ekf(self.model, t_step=t_step)

        self._tvp_template_mpc = self.mpc.get_tvp_template()
        self._horizon = self.mpc.settings.n_horizon + 1
        self._sp_cm = 0.0
        self._cycle = None
        self._t_now_s = 0.0
        self.mpc.set_tvp_fun(self._tvp_fun_mpc)
        self.mpc.setup()

        x0 = np.array([[1.0], [0.0], [0.0]])  # h1, h2, e_int, in cm
        self.ekf.P0 = np.diag([0.5 ** 2, 1.0 ** 2, 10.0 ** 2])
        self.ekf.x0 = x0
        self.ekf.set_initial_guess()
        self.mpc.x0 = x0.flatten()
        self.mpc.set_initial_guess()
        self.x_hat = x0.copy()
        self._last_u_cm3s = 0.0


@dataclass
class ControlResult:
    sp_m: float
    solve_time_ms: float
    converged: bool
    held_last_sp: bool


class UnitController:
    """One MPC instance plus the bumpless-transfer and hold-last-SP logic
    for a single unit. Not thread-safe; the caller in main.py runs each
    unit's controller in its own worker thread.
    """

    def __init__(self, unit_id: int, level_ll_m: float = 0.0, level_hh_m: float = 9.0, cycle=None):
        self.unit_id = unit_id
        self.controller = _PortedWaterTankController()
        self.last_sp_m: float | None = None
        self.needs_bumpless_reset = True
        self.level_ll_m = level_ll_m
        self.level_hh_m = level_hh_m
        self.cycle = cycle
        if cycle is not None:
            self.controller.set_cycle(cycle)

    @property
    def current_cycle_target_m(self) -> float | None:
        """Instantaneous target the cycle is asking for right now, in
        meters, for logging/diagnostics only. None if this unit has no
        cycle (its target is whatever step() is called with instead). The
        MPC's actual horizon preview comes from the cycle's tvp_fun
        sampling in mpc_core.py, not from this single point."""
        if self.cycle is None:
            return None
        return self.cycle.value_at(self.controller._t_now_s) / 100.0

    def request_bumpless_reset(self) -> None:
        """Call when APC.Enabled transitions False -> True for this unit."""
        self.needs_bumpless_reset = True

    def _clip_to_interlock_bounds(self, sp_m: float, level_hh_m: float, level_ll_m: float) -> float:
        # Clip to the ported model's own valid envelope first (see
        # MODEL_LEVEL_MAX_M above), then further tighten against the unit's
        # real interlock band only if that band happens to fall inside the
        # model's envelope (it does not in this demo's fake_plc defaults,
        # see OPEN_QUESTIONS.md, so this is a no-op there but keeps the
        # logic correct if plc/ later ships bounds inside the model range).
        margin = 0.005
        lo = max(MODEL_LEVEL_MIN_M, level_ll_m + margin) if level_ll_m + margin <= MODEL_LEVEL_MAX_M else MODEL_LEVEL_MIN_M
        hi = min(MODEL_LEVEL_MAX_M, level_hh_m - margin) if level_hh_m - margin >= MODEL_LEVEL_MIN_M else MODEL_LEVEL_MAX_M
        if hi < lo:
            lo, hi = MODEL_LEVEL_MIN_M, MODEL_LEVEL_MAX_M
        return max(lo, min(hi, sp_m))

    def step(self, target_level_m: float, level_measured_m: float, level_hh_m: float, level_ll_m: float) -> ControlResult:
        if self.needs_bumpless_reset:
            self.controller.reset_to_measurement(level_measured_m)
            self.needs_bumpless_reset = False
            if self.last_sp_m is None:
                self.last_sp_m = level_measured_m

        result: MPCResult = self.controller.step(setpoint_m=target_level_m, level_measured_m=level_measured_m)

        if not result.converged:
            # Hold the last good setpoint, do not write a fresh one derived
            # from a failed solve.
            held_sp = self.last_sp_m if self.last_sp_m is not None else level_measured_m
            return ControlResult(sp_m=held_sp, solve_time_ms=result.solve_time_ms, converged=False, held_last_sp=True)

        sp_m = self._extract_predicted_sp_m(fallback_m=self.last_sp_m or level_measured_m)
        sp_m = self._clip_to_interlock_bounds(sp_m, level_hh_m, level_ll_m)
        self.last_sp_m = sp_m
        return ControlResult(sp_m=sp_m, solve_time_ms=result.solve_time_ms, converged=True, held_last_sp=False)

    def _extract_predicted_sp_m(self, fallback_m: float) -> float:
        try:
            prediction_cm = self.controller.mpc.data.prediction(("_x", "h2"))
            # Shape from do-mpc 5.1.1: (n_scenarios, n_horizon+1, n_elements).
            # Take the near-term point (index PREDICTION_HORIZON_INDEX along
            # the horizon axis), first (only) scenario and element.
            value_cm = float(prediction_cm[0, PREDICTION_HORIZON_INDEX, 0])
            return value_cm / 100.0
        except Exception:
            return fallback_m
