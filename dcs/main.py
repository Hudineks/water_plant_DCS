"""DCS supervisory process entry point.

Runs three concurrent things:
  1. One UnitClient per PLC endpoint in PLC_ENDPOINTS (OPC UA client side,
     opc_client.py), each reconnecting on its own.
  2. One GlobalServer (global_server.py), the DCS's own OPC UA server
     exposing APC.Enabled/SolveTime_ms/Status to the HMI.
  3. The 1 s control loop below: read cached PV + heartbeat per unit, run
     each unit's MPC in a worker thread (do-mpc/ipopt is blocking, not
     async), write PID.SP for units that converged, hold the last SP for
     units that did not, log everything to the historian.

No tag exists in tags.yaml for an operator-entered APC target level (only
PID.SP, which the DCS itself writes, and Level.SP, which is a PLC-reported
value). Each unit's setpoint source (a cycle CSV for a real MPC horizon
preview, or a fixed constant) comes from dcs/config.py's
unit_setpoint_sources, defaulting to Unit1=step cycle, Unit2=ramp cycle,
Unit3=constant. See OPEN_QUESTIONS.md.
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


class UnitRuntime:
    def __init__(self, unit_id: int, client: UnitClient, setpoint_source):
        self.unit_id = unit_id
        self.client = client

        if isinstance(setpoint_source, str):
            cycle = SetpointCycle.from_csv(ROOT / setpoint_source)
            self.constant_target_m: float | None = None
            logger.info("Unit%d: setpoint source is cycle '%s' (period %.0fs)", unit_id, cycle.name, cycle.period_s)
        else:
            cycle = None
            self.constant_target_m = float(setpoint_source)
            logger.info("Unit%d: setpoint source is constant target %.3f m", unit_id, self.constant_target_m)

        self.controller = UnitController(unit_id, cycle=cycle)
        self.was_enabled = False
        self.last_solve_time_ms = 0.0
        self.last_converged = True

    def nominal_target_m(self) -> float:
        """The instantaneous target to pass into step() for bookkeeping.
        For a cycle-driven unit this is only used for logging/fallback,
        the horizon preview itself comes from the cycle's own tvp_fun
        sampling (see controller_wrapper.py / mpc_core.py), not from this
        single point."""
        if self.constant_target_m is not None:
            return self.constant_target_m
        target = self.controller.current_cycle_target_m
        return target if target is not None else 0.15


async def control_loop(cfg: Config, clients: list[UnitClient], global_server: GlobalServer, historian: Historian) -> None:
    watchdog = HeartbeatWatchdog(cfg.heartbeat_stall_cycles)
    runtimes = [
        UnitRuntime(i + 1, clients[i], cfg.unit_setpoint_sources[i + 1])
        for i in range(len(clients))
    ]
    executor = ThreadPoolExecutor(max_workers=max(1, len(clients)), thread_name_prefix="mpc-solve")

    loop = asyncio.get_event_loop()

    try:
        while True:
            cycle_start = time.monotonic()
            enabled = await global_server.read_enabled()

            alive_runtimes = []
            for rt in runtimes:
                heartbeat = rt.client.get_cached("Status.Heartbeat")
                is_alive = watchdog.observe(rt.unit_id, heartbeat) and rt.client.connected
                if is_alive:
                    alive_runtimes.append(rt)
                else:
                    logger.warning("Unit%d: dropped from optimization loop (dead heartbeat or disconnected)", rt.unit_id)

            solve_futures = {}
            for rt in alive_runtimes:
                pv = rt.client.get_cached("Level.PV")
                if pv is None:
                    continue

                if enabled and not rt.was_enabled:
                    rt.controller.request_bumpless_reset()
                rt.was_enabled = enabled

                if not enabled:
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

            solving_units = list(step_futures.keys())
            if not enabled:
                status = "DISABLED"
                solve_time_ms = 0.0
            elif len(alive_runtimes) < len(runtimes):
                status = "DEGRADED"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)
            elif any(not runtimes[i].last_converged for i in range(len(runtimes)) if runtimes[i].unit_id in solving_units):
                status = "SOLVER_FAIL"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)
            else:
                status = "OK"
                solve_time_ms = max((rt.last_solve_time_ms for rt in alive_runtimes), default=0.0)

            await global_server.publish_status(solve_time_ms, status)
            historian.log_values("Global", {"APC.Enabled": enabled, "APC.SolveTime_ms": solve_time_ms, "APC.Status": status})

            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, cfg.cycle_s - elapsed))
    finally:
        executor.shutdown(wait=False)


async def main() -> None:
    cfg = load_config()
    logger.info("Starting DCS: %d units, cycle=%.1fs, server=%s", len(cfg.plc_endpoints), cfg.cycle_s, cfg.dcs_server_endpoint)

    unit_ids = list(range(1, len(cfg.plc_endpoints) + 1))
    global_server = GlobalServer(cfg.dcs_server_endpoint, unit_ids=unit_ids)
    await global_server.start()

    historian = Historian(cfg.historian_db_path)

    clients = [
        UnitClient(unit_id=i + 1, endpoint=endpoint, reconnect_backoff_s=cfg.reconnect_backoff_s)
        for i, endpoint in enumerate(cfg.plc_endpoints)
    ]
    client_tasks = [asyncio.create_task(c.run()) for c in clients]

    try:
        await control_loop(cfg, clients, global_server, historian)
    finally:
        for c in clients:
            await c.stop()
        for t in client_tasks:
            t.cancel()
        await global_server.stop()
        historian.close()


if __name__ == "__main__":
    asyncio.run(main())
