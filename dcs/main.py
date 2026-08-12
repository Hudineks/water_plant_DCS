"""DCS supervisory process entry point.

Runs three concurrent things:
  1. One UnitClient per PLC endpoint in PLC_ENDPOINTS (OPC UA client side,
     opc_client.py), each reconnecting on its own.
  2. One GlobalServer (global_server.py), the DCS's own OPC UA server
     exposing APC.Enabled/SolveTime_ms/Status (now a derived readout, see
     below) plus each unit's live Control.CycleName/ManualTargetM and
     Diagnostics.H1_Estimated.
  3. The 1 s control loop below: read cached PV + heartbeat per unit, read
     each unit's live setpoint-source selection, run each unit's MPC in a
     worker thread (do-mpc/ipopt is blocking, not async) for units whose
     selection isn't "off", write PID.SP for units that converged, hold
     the last SP for units that did not, log everything to the historian.

Per-unit setpoint source (cycle CSV for real MPC horizon preview, or a
manual constant, or off) is operator-controlled at runtime via
Control.CycleName/Control.ManualTargetM on the DCS's own OPC UA server
(dcs/global_server.py), seeded at startup from dcs/config.py's
unit_setpoint_sources so a fresh process reproduces the project's default
demo (Unit1=step, Unit2=ramp, Unit3=manual) with no operator action
needed. The global APC.Enabled/APC.Status/APC.SolveTime_ms tags are
derived each cycle from the union of per-unit selections (true whenever at
least one unit isn't "off") rather than being an independent operator
input -- see dcs/README.md. No tag exists in tags.yaml for any of this
(only PID.SP, which the DCS itself writes, and Level.SP, a PLC-reported
value); see OPEN_QUESTIONS.md.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cycles.loader import SetpointCycle
from dcs.config import Config, load_config
from dcs.controller_wrapper import UnitController
from dcs.global_server import GlobalServer
from dcs.historian import Historian
from dcs.opc_client import UnitClient
from dcs.watchdog import HeartbeatWatchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dcs.main")

# Canonical cycle files offered by the HMI's Step/Ramp dropdown options.
# Fixed paths, not configurable per unit anymore now that selection is
# live -- DCS_UNIT{n}_CYCLE/_TARGET_M (dcs/config.py) still control each
# unit's *initial* selection at startup, not which files exist to choose
# from.
CYCLE_FILES = {
    "step": ROOT / "cycles" / "step_response.csv",
    "ramp": ROOT / "cycles" / "ramp_response.csv",
}


def _seed_from_source(source) -> tuple[str, float]:
    """Maps a dcs/config.py unit_setpoint_sources entry to the
    (cycle_name, manual_target_m) pair GlobalServer seeds its Control
    nodes with at startup."""
    if isinstance(source, str):
        name = Path(source).stem
        if "step" in name:
            return "step", 0.15
        if "ramp" in name:
            return "ramp", 0.15
        return "manual", 0.15
    return "manual", float(source)


class UnitRuntime:
    def __init__(self, unit_id: int, client: UnitClient):
        self.unit_id = unit_id
        self.client = client
        self.controller = UnitController(unit_id)
        self.manual_target_m = 0.15
        self.active = False
        self.was_alive = False
        self.last_solve_time_ms = 0.0
        self.last_converged = True

    def nominal_target_m(self) -> float:
        """The instantaneous target to pass into step() for bookkeeping.
        For a cycle-driven unit this is only used for logging/fallback,
        the horizon preview itself comes from the cycle's own tvp_fun
        sampling (see controller_wrapper.py / mpc_core.py), not from this
        single point. For a manual unit it is the actual, only target."""
        if self.controller.cycle is not None:
            target = self.controller.current_cycle_target_m
            if target is not None:
                return target
        return self.manual_target_m


async def control_loop(
    cfg: Config,
    clients: list[UnitClient],
    global_server: GlobalServer,
    historian: Historian,
    cycles_by_name: dict[str, SetpointCycle],
) -> None:
    watchdog = HeartbeatWatchdog(cfg.heartbeat_stall_cycles)
    runtimes = [UnitRuntime(i + 1, clients[i]) for i in range(len(clients))]
    executor = ThreadPoolExecutor(max_workers=max(1, len(clients)), thread_name_prefix="mpc-solve")

    loop = asyncio.get_event_loop()

    try:
        while True:
            cycle_start = time.monotonic()

            alive_runtimes = []
            for rt in runtimes:
                heartbeat = rt.client.get_cached("Status.Heartbeat")
                is_alive = watchdog.observe(rt.unit_id, heartbeat) and rt.client.connected
                if is_alive:
                    if not rt.was_alive:
                        # Unit just (re)connected, e.g. its plc.unit process
                        # was restarted. Its real level is whatever the new
                        # process started at, unrelated to whatever this
                        # controller's EKF/MPC state (x_hat) was tracking
                        # before the gap. Without a reset here, the next
                        # solve mixes a stale internal state estimate with a
                        # fresh, unrelated measurement, which can produce a
                        # bad PID.SP and drive the real plant toward its LL
                        # interlock exactly like the bug PREDICTION_HORIZON_INDEX=25
                        # already fixes for a too-shallow horizon (see
                        # OPEN_QUESTIONS.md) -- same symptom, different cause.
                        logger.info("Unit%d: (re)connected, forcing bumpless reset", rt.unit_id)
                        rt.controller.request_bumpless_reset()
                    rt.was_alive = True
                    alive_runtimes.append(rt)
                else:
                    rt.was_alive = False
                    logger.warning("Unit%d: dropped from optimization loop (dead heartbeat or disconnected)", rt.unit_id)

            solve_futures = {}
            for rt in alive_runtimes:
                pv = rt.client.get_cached("Level.PV")

                cycle_name, manual_target_m = await global_server.read_unit_control(rt.unit_id)
                if cycle_name != rt.controller.cycle_name:
                    logger.info("Unit%d: setpoint source changed to '%s'", rt.unit_id, cycle_name)
                rt.controller.set_setpoint_source(cycle_name, cycles_by_name)
                rt.manual_target_m = manual_target_m
                rt.active = cycle_name != "off"

                if pv is None or not rt.active:
                    continue

                ll_hh_future = rt.client.read_bounds()
                solve_futures[rt.unit_id] = (rt, pv, ll_hh_future)

            # Resolve bounds (cheap OPC UA reads) then dispatch the blocking
            # MPC solves to the thread pool so units run in parallel and one
            # slow solve does not serialize behind another.
            step_futures = {}
            for unit_id, (rt, pv, ll_hh_future) in solve_futures.items():
                ll_m, hh_m = await ll_hh_future
                fut = loop.run_in_executor(executor, rt.controller.step, rt.nominal_target_m(), pv, hh_m, ll_m)
                step_futures[unit_id] = (rt, fut)

            deadline = cycle_start + cfg.solve_budget_s
            for unit_id, (rt, fut) in step_futures.items():
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.warning("Unit%d: MPC solve exceeded cycle budget, holding last SP", unit_id)
                    rt.last_converged = False
                    rt.last_solve_time_ms = cfg.solve_budget_s * 1000.0
                    continue

                rt.last_converged = result.converged
                rt.last_solve_time_ms = result.solve_time_ms
                if not result.converged:
                    logger.warning("Unit%d: MPC did not converge, holding last SP (%.3f m)", unit_id, result.sp_m)
                try:
                    await rt.client.write_pid_sp(result.sp_m)
                except Exception as exc:
                    logger.warning("Unit%d: failed to write PID.SP (%s)", unit_id, exc)

                h1_estimated_m = float(rt.controller.controller.x_hat[0, 0]) / 100.0
                await global_server.publish_diagnostics(unit_id, h1_estimated_m)

            # Historian: log every reachable unit's full tag set plus the
            # global APC tags, once per cycle.
            for rt in runtimes:
                snapshot = await rt.client.read_snapshot()
                if snapshot:
                    historian.log_values(f"Unit{rt.unit_id}", snapshot)

            solving_units = set(step_futures.keys())
            enabled_any = any(rt.active for rt in alive_runtimes)
            if not enabled_any:
                status = "DISABLED"
                solve_time_ms = 0.0
            elif len(alive_runtimes) < len(runtimes):
                status = "DEGRADED"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)
            elif any(not rt.last_converged for rt in alive_runtimes if rt.unit_id in solving_units):
                status = "SOLVER_FAIL"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)
            else:
                status = "OK"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)

            await global_server.publish_status(enabled_any, solve_time_ms, status)
            historian.log_values("Global", {"APC.Enabled": enabled_any, "APC.SolveTime_ms": solve_time_ms, "APC.Status": status})

            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, cfg.cycle_s - elapsed))
    finally:
        executor.shutdown(wait=False)


async def main() -> None:
    cfg = load_config()
    logger.info("Starting DCS: %d units, cycle=%.1fs, server=%s", len(cfg.plc_endpoints), cfg.cycle_s, cfg.dcs_server_endpoint)

    cycles_by_name = {name: SetpointCycle.from_csv(path) for name, path in CYCLE_FILES.items()}

    unit_ids = list(range(1, len(cfg.plc_endpoints) + 1))
    unit_control_seed = {
        unit_id: _seed_from_source(cfg.unit_setpoint_sources.get(unit_id, 0.15))
        for unit_id in unit_ids
    }
    global_server = GlobalServer(cfg.dcs_server_endpoint, unit_ids=unit_ids, unit_control_seed=unit_control_seed)
    await global_server.start()

    historian = Historian(cfg.historian_db_path)

    clients = [
        UnitClient(unit_id=i + 1, endpoint=endpoint, reconnect_backoff_s=cfg.reconnect_backoff_s)
        for i, endpoint in enumerate(cfg.plc_endpoints)
    ]
    client_tasks = [asyncio.create_task(c.run()) for c in clients]

    try:
        await control_loop(cfg, clients, global_server, historian, cycles_by_name)
    finally:
        for c in clients:
            await c.stop()
        for t in client_tasks:
            t.cancel()
        await global_server.stop()
        historian.close()


if __name__ == "__main__":
    asyncio.run(main())
