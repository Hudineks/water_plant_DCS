"""Wraps reference.water_mpc.WaterTankController for one unit and derives
the PID.SP value that gets written to the PLC.

Mapping from MPC internals to PID.SP (documented in dcs/README.md):

reference/water_mpc/mpc_core.py's WaterTankController.step() already
computes an optimal flow every solve (MPCResult.flow_cm3s) -- that is
literally what the MPC optimizes for. PID.SP is that flow, clipped to the
model's own actuator bounds, written straight through. Per tags.yaml this
project's cascade still never writes an actuator command from the
supervisory layer directly: the PLC converts this flow into Pump.CMD
itself (a fixed calibration curve, no closed loop, see plc/scan_engine.py)
in CASCADE mode, so PID.SP stays a setpoint, not a valve/pump write.

Earlier versions of this file wrote a *level* point instead, extracted
from the MPC's predicted h2 trajectory at a tuned horizon index, and threw
flow_cm3s away entirely. That translation was both unnecessary work and a
real source of instability: the EKF inside step() assumes whatever flow
it just computed was the flow actually applied to the plant
(`u_next=u_val_cm3s` in mpc_core.py), but the real applied flow was
whatever a separate local level-PID's gains produced, which is a
different number. That mismatch could drift the internal state estimate
away from reality over a long run. Writing flow_cm3s directly removes the
mismatch by construction: the PLC's flow-to-Pump.CMD map is exact and
linear, so what gets applied is (net of one control cycle's delay)
exactly what the MPC commanded, matching the EKF's own assumption instead
of fighting it. See OPEN_QUESTIONS.md for the two collapse bugs this
design replaces.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reference.water_mpc.mpc_core import (  # noqa: E402
    MPCResult,
    TIME_STEP_S,
    U_MAX,
    U_MIN,
    WaterTankController,
    build_ekf,
    build_mpc,
    build_model,
)


class _PortedWaterTankController(WaterTankController):
    """WaterTankController with its __init__ adapted for do-mpc 5.1.1
    (the version pinned in requirements.txt). The original __init__ from
    reference/water_mpc/mpc_core.py (frozen, not edited) has a problem
    against this do-mpc version, fixed here without touching the model,
    objective, constraints or solver settings built by
    build_model()/build_mpc()/build_ekf():

    mpc.set_tvp_fun() validates its argument by calling it immediately.
    The tvp function reads self._sp_cm, which the original __init__ only
    assigns *after* calling set_tvp_fun, raising AttributeError. Fixed by
    assigning self._sp_cm = 0.0 before set_tvp_fun().

    reset_to_measurement() and step() are inherited unchanged.
    """

    def __init__(self, n_horizon: int = 40, t_step: float = TIME_STEP_S):
        self.t_step = t_step
        self.model = build_model()
        self.mpc = build_mpc(self.model, n_horizon=n_horizon, t_step=t_step)
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
    sp_flow_cm3s: float
    solve_time_ms: float
    converged: bool
    held_last_sp: bool


class UnitController:
    """One MPC instance plus the bumpless-transfer and hold-last-SP logic
    for a single unit. Not thread-safe; the caller in main.py runs each
    unit's controller in its own worker thread.
    """

    def __init__(self, unit_id: int, level_ll_m: float = 0.0, level_hh_m: float = 9.0):
        self.unit_id = unit_id
        self.controller = _PortedWaterTankController()
        self.last_flow_cm3s: float | None = None
        self.needs_bumpless_reset = True
        self.level_ll_m = level_ll_m
        self.level_hh_m = level_hh_m
        self.cycle = None
        # None means "not yet told what to run" -- distinct from "off",
        # so the first real value read from Control.CycleName (even "off")
        # is applied via set_setpoint_source() instead of being skipped as
        # a no-op change. See dcs/main.py's control_loop.
        self.cycle_name: str | None = None

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

    def set_setpoint_source(self, cycle_name: str, cycles: dict[str, object]) -> None:
        """Switch which setpoint source drives this unit: cycle_name is one
        of "off" / "step" / "ramp" / "manual" (see
        dcs/global_server.py's Control.CycleName). `cycles` maps "step"/
        "ramp" to preloaded SetpointCycle instances; "off"/"manual" have no
        cycle object (None), the difference between them is handled by the
        caller (main.py skips solving entirely for "off").

        No-op if the source hasn't changed since the last call, so a
        steady selection doesn't reset every control cycle. Changing the
        source triggers a bumpless reset, since switching from tracking
        one trajectory to another (or to/from manual) is exactly the kind
        of transition reset_to_measurement() exists to smooth.
        """
        if cycle_name == self.cycle_name:
            return
        self.cycle_name = cycle_name
        self.cycle = cycles.get(cycle_name)
        self.controller.set_cycle(self.cycle)
        self.request_bumpless_reset()

    def request_bumpless_reset(self) -> None:
        """Call when a unit starts being solved again: APC.Enabled
        transitions False -> True (legacy global path) or, per-unit, when
        set_setpoint_source() changes what this unit is tracking, or the
        unit's OPC UA client reconnects after a gap (see dcs/main.py's
        control_loop, was_alive tracking)."""
        self.needs_bumpless_reset = True

    @staticmethod
    def _clip_to_flow_bounds(flow_cm3s: float) -> float:
        # Physical actuator bounds only (0..U_MAX cm3/s, the reference
        # model's own pump limits). No level-based clipping: the MPC's
        # own state constraints (mpc.bounds['upper','_x','h2'] in
        # build_mpc()) already make it choose low flow near its level
        # ceiling as an emergent property of the optimization, so a
        # manual level-based post-hoc clip on the flow output was never
        # doing useful work here. Level.LL/HH stay enforced independently
        # by the PLC's own interlocks (plc/interlocks.py).
        return max(U_MIN, min(U_MAX, flow_cm3s))

    def step(self, target_level_m: float, level_measured_m: float, level_hh_m: float, level_ll_m: float) -> ControlResult:
        if self.needs_bumpless_reset:
            self.controller.reset_to_measurement(level_measured_m)
            self.needs_bumpless_reset = False

        result: MPCResult = self.controller.step(setpoint_m=target_level_m, level_measured_m=level_measured_m)

        if not result.converged:
            # Hold the last good flow, do not write a fresh one derived
            # from a failed solve.
            held_flow = self.last_flow_cm3s if self.last_flow_cm3s is not None else 0.0
            return ControlResult(sp_flow_cm3s=held_flow, solve_time_ms=result.solve_time_ms, converged=False, held_last_sp=True)

        flow_cm3s = self._clip_to_flow_bounds(result.flow_cm3s)
        self.last_flow_cm3s = flow_cm3s
        return ControlResult(sp_flow_cm3s=flow_cm3s, solve_time_ms=result.solve_time_ms, converged=True, held_last_sp=False)
