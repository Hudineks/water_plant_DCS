"""Standalone linear-ish MPC + EKF for a single water tank level loop.

This is a port of src/models/mpc_water_tank_controller.py from the original
Vodarna project. Removed: DAQ (nidaqmx) I/O, matplotlib live plotting, CSV
recipe/cycle playback, GUI threading. Kept: the do-mpc model, the MPC
objective/constraints/solver settings, the EKF, and the anti-windup term,
unchanged in substance from the original.

Units: the original model works in centimeters (tank geometry was specified
in mm/cm). tags.yaml uses meters for Level.PV/SP. WaterTankController exposes
its public step() interface in meters and converts internally, so the numeric
core below stays a faithful port of the original cm-based model.

This module has no OPC UA dependency. dcs/ imports it and wires it to the
OPC UA client loop.
"""
from __future__ import annotations

from dataclasses import dataclass

import casadi as ca
import do_mpc
import numpy as np

# -----------------------------
# Physical parameters (tank geometry), same values as the original rig
# -----------------------------
D_TANK_MM = 50.0
D_HOLE_MM = 4.0
CD = 0.61
G_CM = 981.0  # cm/s^2

D_TANK_CM = D_TANK_MM / 10.0
D_HOLE_CM = D_HOLE_MM / 10.0

F_AREA = np.pi * (D_TANK_CM / 2.0) ** 2          # tank cross-section, cm^2
S_HOLE = np.pi * (D_HOLE_CM / 2.0) ** 2          # outlet hole area, cm^2
K_OUT = CD * S_HOLE * np.sqrt(2.0 * G_CM)        # outlet coefficient, cm^2.5/s
K_TRANSFER = K_OUT                                # tank1 -> tank2 transfer coefficient

TIME_STEP_S = 1.0
EPS = 1e-6

U_MIN, U_MAX = 0.0, 17.0          # pump flow bounds, cm^3/s
H1_MAX, H2_MAX = 15.0, 20.0       # state bounds, cm

Q_INT = 0.0                       # integral term weight in the objective
ANTI_WINDUP_ENABLED = True
ANTI_WINDUP_THRESHOLD = 0.01


def build_model() -> do_mpc.model.Model:
    model = do_mpc.model.Model(model_type="continuous")

    h1 = model.set_variable(var_type="_x", var_name="h1")
    h2 = model.set_variable(var_type="_x", var_name="h2")
    e_int = model.set_variable(var_type="_x", var_name="e_int")
    q0 = model.set_variable(var_type="_u", var_name="q0")
    sp = model.set_variable(var_type="_tvp", var_name="sp")

    model.set_rhs("h1", (q0 - K_OUT * ca.sqrt(ca.fmax(h1, 0) + EPS)) / F_AREA)
    model.set_rhs("h2", (K_TRANSFER * ca.sqrt(ca.fmax(h1, 0) + EPS) - K_OUT * ca.sqrt(ca.fmax(h2, 0) + EPS)) / F_AREA)

    error = h2 - sp
    if ANTI_WINDUP_ENABLED:
        dist_from_low = q0 - U_MIN
        dist_from_high = U_MAX - q0
        aw_low = ca.fmax(0.0, ca.fmin(1.0,
            ca.exp(-ca.fmax(0, dist_from_low - ANTI_WINDUP_THRESHOLD) / (ANTI_WINDUP_THRESHOLD + EPS)) *
            ca.fmax(0, -error) / (ca.fabs(error) + EPS + 0.1)
        ))
        aw_high = ca.fmax(0.0, ca.fmin(1.0,
            ca.exp(-ca.fmax(0, dist_from_high - ANTI_WINDUP_THRESHOLD) / (ANTI_WINDUP_THRESHOLD + EPS)) *
            ca.fmax(0, error) / (ca.fabs(error) + EPS + 0.1)
        ))
        aw_factor = 1.0 - 0.9 * ca.fmax(aw_low, aw_high)
    else:
        aw_factor = 1.0

    model.set_rhs("e_int", aw_factor * (h2 - sp))
    model.set_meas("h2_meas", h2)
    model.setup()
    return model


def build_mpc(model: do_mpc.model.Model, n_horizon: int = 40, t_step: float = TIME_STEP_S) -> do_mpc.controller.MPC:
    mpc = do_mpc.controller.MPC(model)
    mpc.set_param(n_horizon=n_horizon, t_step=t_step)

    h2 = model.x["h2"]
    h1 = model.x["h1"]
    e_int = model.x["e_int"]
    sp = model.tvp["sp"]

    mpc.set_objective(
        mterm=(h2 - sp) ** 2,
        lterm=(h2 - sp) ** 2 + 0.0 * h1 ** 2 + Q_INT * e_int ** 2,
    )
    mpc.set_rterm(q0=0.02)

    mpc.bounds["lower", "_u", "q0"] = U_MIN
    mpc.bounds["upper", "_u", "q0"] = U_MAX
    mpc.bounds["lower", "_x", "h1"] = 0.0
    mpc.bounds["upper", "_x", "h1"] = H1_MAX
    mpc.bounds["lower", "_x", "h2"] = 0.0
    mpc.bounds["upper", "_x", "h2"] = H2_MAX
    mpc.bounds["lower", "_x", "e_int"] = -1000.0
    mpc.bounds["upper", "_x", "e_int"] = 1000.0

    mpc.settings.supress_ipopt_output = True
    mpc.settings.nlpsol_opts = {
        "ipopt.max_iter": 15,
        "ipopt.max_cpu_time": 0.3,
        "ipopt.tol": 5e-3,
        "ipopt.print_level": 0,
        "ipopt.acceptable_tol": 1e-2,
        "ipopt.acceptable_iter": 2,
        "ipopt.hessian_approximation": "limited-memory",
        "ipopt.warm_start_init_point": "yes",
        "ipopt.mu_strategy": "adaptive",
        "ipopt.nlp_scaling_method": "gradient-based",
    }

    return mpc


def build_ekf(model: do_mpc.model.Model, t_step: float = TIME_STEP_S) -> do_mpc.estimator.EKF:
    ekf = do_mpc.estimator.EKF(model=model)
    ekf.settings.t_step = t_step
    tvp_template = ekf.get_tvp_template()

    def tvp_fun(_t_now):
        return tvp_template

    p_template = ekf.get_p_template()

    def p_fun(_t_now):
        return p_template

    ekf.set_tvp_fun(tvp_fun)
    ekf.set_p_fun(p_fun)
    ekf.setup()
    return ekf


@dataclass
class MPCResult:
    flow_cm3s: float
    solve_time_ms: float
    converged: bool


def _dm_to_array(value) -> np.ndarray:
    if hasattr(value, "full"):
        return value.full()
    return np.array(value, dtype=float).reshape((-1, 1))


class WaterTankController:
    """Public interface used by dcs/. Works in meters and seconds.

    Call step(setpoint_m, level_measured_m) once per control cycle. It runs
    the EKF update from the last cycle's flow command and the new
    measurement, then re-solves the MPC and returns the new flow command.
    The caller is responsible for converting the flow command into whatever
    the actuator understands and for deciding what to do when converged is
    False (this class does not fall back on its own, see dcs/ watchdog logic).
    """

    def __init__(self, n_horizon: int = 40, t_step: float = TIME_STEP_S):
        self.t_step = t_step
        self.model = build_model()
        self.mpc = build_mpc(self.model, n_horizon=n_horizon, t_step=t_step)
        self.ekf = build_ekf(self.model, t_step=t_step)

        self._tvp_template_mpc = self.mpc.get_tvp_template()
        self._horizon = self.mpc.settings.n_horizon + 1
        self._sp_cm = 0.0
        # Optional setpoint-trajectory source, see set_cycle(). Any object
        # with a value_at(t_now_s, t_start_s=0.0) -> float (cm) method
        # works; cycles.loader.SetpointCycle is the one this project ships.
        # Must be set before set_tvp_fun(), which calls the function
        # immediately to validate its return type.
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

    def set_cycle(self, cycle) -> None:
        """Give the MPC a real setpoint trajectory to preview across its
        horizon, instead of a single flat target (ported from the original
        rig's load_cycle_to_mpc/tvp_fun mechanism). Pass None to go back to
        a flat scalar setpoint driven by step()'s setpoint_m argument.

        Resets the controller's internal synthetic clock to 0, so the
        cycle's phase always starts at its own t=0 from this call (matches
        reset_to_measurement()'s bumpless-transfer restart, see below).
        """
        self._cycle = cycle
        self._t_now_s = 0.0

    def _tvp_fun_mpc(self, _t_now):
        if self._cycle is not None:
            sp_values = [
                self._cycle.value_at(self._t_now_s + k * self.t_step)
                for k in range(self._horizon)
            ]
            self._tvp_template_mpc["_tvp", :] = sp_values
        else:
            self._tvp_template_mpc["_tvp", :] = [self._sp_cm] * self._horizon
        return self._tvp_template_mpc

    def reset_to_measurement(self, level_measured_m: float) -> None:
        """Bumpless-transfer entry point: seed the internal state estimate
        with the current plant measurement so re-enabling APC does not
        cause a setpoint jump. Call this once, right before the first
        step() after APC.Enabled flips to true. Also restarts any set
        cycle's phase at t=0, the simplest and safest choice for a
        scripted demo run rather than tracking true wall-clock phase
        across enable/disable.
        """
        h2_cm = level_measured_m * 100.0
        x0 = np.array([[h2_cm], [h2_cm], [0.0]])
        self.ekf.x0 = x0
        self.mpc.x0 = x0.flatten()
        self.mpc.set_initial_guess()
        self.x_hat = x0.copy()
        self._t_now_s = 0.0

    def step(self, setpoint_m: float, level_measured_m: float) -> MPCResult:
        import time

        self._sp_cm = setpoint_m * 100.0
        h2_meas_cm = level_measured_m * 100.0

        self.mpc.x0 = self.x_hat.flatten()
        try:
            self.mpc.set_initial_guess()
        except Exception:
            pass

        start = time.perf_counter()
        converged = True
        try:
            u = self.mpc.make_step(self.x_hat.flatten())
            u_val_cm3s = float(np.asarray(u).ravel()[0])
        except Exception:
            converged = False
            u_val_cm3s = self._last_u_cm3s

        solve_time_ms = (time.perf_counter() - start) * 1000.0

        y_next = np.array([[h2_meas_cm]])
        u_next = np.array([[u_val_cm3s]])
        try:
            x_hat_dm = self.ekf.make_step(
                y_next=y_next, u_next=u_next,
                Q_k=np.diag([1e-2, 1e-2, 1e-2]), R_k=np.diag([0.05 ** 2]),
            )
            self.x_hat = np.clip(
                _dm_to_array(x_hat_dm), [[0.0], [0.0], [-np.inf]], [[H2_MAX * 2], [H2_MAX * 2], [np.inf]]
            )
            self.ekf.x0 = self.x_hat
        except Exception:
            pass

        self._last_u_cm3s = u_val_cm3s
        self._t_now_s += self.t_step
        return MPCResult(flow_cm3s=u_val_cm3s, solve_time_ms=solve_time_ms, converged=converged)
