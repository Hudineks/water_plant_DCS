"""Scan-cycle engine for one PLC unit.

Ties the tank model, PID, and interlock logic together in the order a real
PLC scan runs: read inputs -> execute logic -> write outputs. Pure Python,
no OPC UA dependency, so it can be driven directly from a test or from the
asyncua server loop in unit.py.

Mode notes (see OPEN_QUESTIONS.md for the full reasoning): tags.yaml exposes
PID.Mode and Pump.CMD as read-only to every external client, there is no tag
a DCS or HMI can write to select AUTO/MAN/CASCADE or to drive the pump by
hand. So mode changes in this simulator are internal: the unit starts in
whatever mode UNIT_INITIAL_MODE says (default CASCADE) and the only automatic
transition is the SP watchdog dropping CASCADE to AUTO. MAN mode is
implemented (freezes Pump.CMD at its last value) for completeness and for
tests, but nothing in the current contract lets an external client trigger it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .interlocks import InterlockLogic
from .model import TankModel
from .pid import PID

logger = logging.getLogger("plc.scan_engine")

SP_STALE_TIMEOUT_S = 5.0


@dataclass
class ScanOutputs:
    level_pv: float
    level_sp: float
    level_hh: float
    level_ll: float
    pump_cmd: float
    pump_fb: float
    pump_running: bool
    valve_cmd: float
    valve_fb: float
    pid_sp: float
    pid_out: float
    pid_mode: str
    interlock_trip: bool
    interlock_reason: str
    heartbeat: int
    scan_time_ms: float


class ScanEngine:
    def __init__(
        self,
        hh: float,
        ll: float,
        local_sp: float,
        valve_cmd_pct: float = 30.0,
        pid_kp: float = 40.0,
        pid_ki: float = 5.0,
        pid_kd: float = 0.0,
        area_m2: float = 2.0,
        pump_max_flow_m3s: float = 0.05,
        valve_cv: float = 0.03,
        initial_level_m: float = 1.5,
        initial_mode: str = "CASCADE",
    ):
        self.model = TankModel(
            area_m2=area_m2,
            pump_max_flow_m3s=pump_max_flow_m3s,
            valve_cv=valve_cv,
            level_m=initial_level_m,
        )
        self.pid = PID(kp=pid_kp, ki=pid_ki, kd=pid_kd, out_min=0.0, out_max=100.0)
        self.interlock = InterlockLogic(hh=hh, ll=ll)

        self.local_sp = local_sp
        self.mode = initial_mode
        self.valve_cmd_pct = valve_cmd_pct  # disturbance input, not controlled by the PID

        self._last_cascade_sp = local_sp
        self._last_pump_cmd = 0.0
        self._man_pump_cmd = 0.0
        self.heartbeat = 0

    def reset_interlock(self) -> None:
        if self.interlock.tripped:
            logger.info("Interlock manually reset (was: %s)", self.interlock.reason)
        self.interlock.reset()

    def set_man_pump_cmd(self, value: float) -> None:
        """Used only for tests / local operator override, see module docstring."""
        self._man_pump_cmd = max(0.0, min(100.0, value))

    def scan(self, dt_sim_s: float, dcs_sp: float, dcs_sp_age_s: float | None) -> ScanOutputs:
        """Run one scan cycle. dcs_sp/dcs_sp_age_s come from reading PID.SP's
        current value and the wall-clock age of its last write timestamp."""
        t0 = time.perf_counter()

        # --- READ INPUTS ---
        level_pv = self.model.level_m

        # --- EXECUTE LOGIC ---
        if self.mode == "CASCADE":
            if dcs_sp_age_s is not None and dcs_sp_age_s > SP_STALE_TIMEOUT_S:
                logger.warning(
                    "PID.SP stale (%.1fs old), dropping CASCADE to AUTO on last known setpoint %.3f m",
                    dcs_sp_age_s, self._last_cascade_sp,
                )
                self.local_sp = self._last_cascade_sp
                self.mode = "AUTO"
            else:
                self._last_cascade_sp = dcs_sp

        effective_sp = dcs_sp if self.mode == "CASCADE" else self.local_sp

        tripped, reason = self.interlock.evaluate(level_pv, self._last_pump_cmd)

        if tripped:
            pump_cmd = 0.0
            pid_out = 0.0
            self.pid.reset(level_pv)
        elif self.mode == "MAN":
            pump_cmd = self._man_pump_cmd
            pid_out = pump_cmd
        else:
            pid_out = self.pid.compute(effective_sp, level_pv, dt_sim_s)
            pump_cmd = pid_out

        pump_running = pump_cmd > 0.0 and not tripped

        new_level = self.model.step(pump_cmd, self.valve_cmd_pct, dt_sim_s)

        self._last_pump_cmd = pump_cmd
        self.heartbeat += 1
        scan_time_ms = (time.perf_counter() - t0) * 1000.0

        # --- WRITE OUTPUTS ---
        return ScanOutputs(
            level_pv=new_level,
            level_sp=effective_sp,
            level_hh=self.interlock.hh,
            level_ll=self.interlock.ll,
            pump_cmd=pump_cmd,
            pump_fb=pump_cmd,
            pump_running=pump_running,
            valve_cmd=self.valve_cmd_pct,
            valve_fb=self.valve_cmd_pct,
            pid_sp=dcs_sp,
            pid_out=pid_out,
            pid_mode=self.mode,
            interlock_trip=tripped,
            interlock_reason=reason,
            heartbeat=self.heartbeat,
            scan_time_ms=scan_time_ms,
        )
