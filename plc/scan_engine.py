"""Scan-cycle engine for one PLC unit.

Ties the tank model and interlock logic together in the order a real PLC
scan runs: read inputs -> execute logic -> write outputs. Pure Python, no
OPC UA dependency, so it can be driven directly from a test or from the
asyncua server loop in unit.py.

PID.SP is a flow setpoint (cm3/s), not a level setpoint (see tags.yaml's
top-of-file note for why): there is no local closed loop here, only a
static, no-feedback conversion from the commanded flow to Pump.CMD
(matching the real rig, which has no flow sensor either, just a
calibration curve). "PID" in the tag/mode names is a naming holdover from
the contract, not a controller that runs in this file.

Mode notes (see OPEN_QUESTIONS.md for the full reasoning): tags.yaml
exposes PID.Mode and Pump.CMD as read-only to every external client, there
is no tag a DCS or HMI can write to select AUTO/MAN/CASCADE or to drive
the pump by hand. So mode changes in this simulator are internal: the
unit starts in whatever mode UNIT_INITIAL_MODE says (default CASCADE) and
the only automatic transition is the SP watchdog dropping CASCADE to
AUTO. MAN mode is implemented (freezes Pump.CMD at its last value) for
completeness and for tests, but nothing in the current contract lets an
external client trigger it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .interlocks import InterlockLogic
from .model import TankModel

logger = logging.getLogger("plc.scan_engine")

# Generous enough to survive a real DCS's own startup latency (building 3
# do-mpc/casadi MPC controllers takes 10-15s in this project) without
# tripping AUTO before the DCS has ever written a setpoint. A shorter
# value here would make every normal sequential startup (plc/ up first,
# then dcs/) fall back to AUTO immediately, since PID.SP's write-age clock
# starts ticking the moment the PLC seeds its own initial value.
SP_STALE_TIMEOUT_S = 30.0


@dataclass
class ScanOutputs:
    level_pv: float
    level_hh: float
    level_ll: float
    pump_cmd: float
    pump_fb: float
    pump_running: bool
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
        pump_max_flow_cm3s: float = 17.0,
        initial_level_m: float = 0.05,
        initial_mode: str = "CASCADE",
    ):
        initial_level_cm = initial_level_m * 100.0
        self.model = TankModel(
            pump_max_flow_cm3s=pump_max_flow_cm3s,
            h1_cm=initial_level_cm,
            h2_cm=initial_level_cm,
        )
        self.interlock = InterlockLogic(hh=hh, ll=ll)

        self.mode = initial_mode

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
        current value (a flow, cm3/s) and the wall-clock age of its last
        write timestamp."""
        t0 = time.perf_counter()

        # --- READ INPUTS ---
        level_pv = self.model.h2_cm / 100.0  # meters, at the OPC UA boundary
        max_flow = self.model.pump_max_flow_cm3s

        # --- EXECUTE LOGIC ---
        if self.mode == "CASCADE" and dcs_sp_age_s is not None and dcs_sp_age_s > SP_STALE_TIMEOUT_S:
            logger.warning(
                "PID.SP stale (%.1fs old), dropping CASCADE to AUTO: flow -> 0 (fail-safe)",
                dcs_sp_age_s,
            )
            self.mode = "AUTO"

        # AUTO has no configurable fallback: the safe default on a lost
        # cascade is zero flow, not the last commanded value.
        effective_flow_sp = max(0.0, min(max_flow, dcs_sp)) if self.mode == "CASCADE" else 0.0

        tripped, reason = self.interlock.evaluate(level_pv, self._last_pump_cmd)

        if tripped:
            pump_cmd = 0.0
            pid_out = 0.0
        elif self.mode == "MAN":
            pump_cmd = self._man_pump_cmd
            pid_out = pump_cmd
        else:
            pid_out = max(0.0, min(100.0, 100.0 * effective_flow_sp / max_flow))
            pump_cmd = pid_out

        pump_running = pump_cmd > 0.0 and not tripped

        new_level_cm = self.model.step(pump_cmd, dt_sim_s)
        new_level = new_level_cm / 100.0

        self._last_pump_cmd = pump_cmd
        self.heartbeat += 1
        scan_time_ms = (time.perf_counter() - t0) * 1000.0

        # --- WRITE OUTPUTS ---
        return ScanOutputs(
            level_pv=new_level,
            level_hh=self.interlock.hh,
            level_ll=self.interlock.ll,
            pump_cmd=pump_cmd,
            pump_fb=pump_cmd,
            pump_running=pump_running,
            pid_sp=dcs_sp,
            pid_out=pid_out,
            pid_mode=self.mode,
            interlock_trip=tripped,
            interlock_reason=reason,
            heartbeat=self.heartbeat,
            scan_time_ms=scan_time_ms,
        )
