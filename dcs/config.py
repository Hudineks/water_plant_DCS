"""Environment configuration for the dcs process. No abstraction beyond
reading env vars with sane defaults, since this is a single deployable
service, not a library.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_endpoints(raw: str) -> list[str]:
    return [e.strip() for e in raw.split(",") if e.strip()]


@dataclass
class Config:
    # opc.tcp:// URLs of the PLC units this DCS instance supervises, one per
    # unit, in unit order (PLC_ENDPOINTS[0] is Unit1, etc). Matches the way
    # tools/fake_plc.py assigns ports: unit N on base_port + N - 1.
    plc_endpoints: list[str] = field(default_factory=list)

    # Endpoint the DCS's own OPC UA server (global tags: APC.*) listens on.
    dcs_server_endpoint: str = "opc.tcp://0.0.0.0:4900/"

    # Control cycle period, matches the MPC's internal t_step (mpc_core.py).
    cycle_s: float = 1.0

    # Per-cycle wall-clock budget for all unit solves combined. If a unit's
    # solve has not returned by the time the cycle budget elapses, that
    # unit's SP is held and APC.Status for that cycle reflects SOLVER_FAIL.
    # ipopt itself is capped per-solve (mpc_core.py ipopt.max_cpu_time=0.3s),
    # but measured wall time per unit is closer to 0.3-0.4s including
    # casadi/do-mpc overhead, and the ThreadPoolExecutor workers do not run
    # fully in parallel (ipopt does not release the GIL for the whole
    # solve), so N units cost roughly N * 0.35s wall time. This budget is
    # sized for 3 units with headroom; tune it (or reduce n_horizon in
    # mpc_core.py, out of scope for a frozen reference) for more units.
    solve_budget_s: float = 2.0

    # A unit is declared dead if Status.Heartbeat has not changed for this
    # many consecutive cycles.
    heartbeat_stall_cycles: int = 5

    historian_db_path: str = "dcs_historian.sqlite3"

    # OPC UA client reconnect backoff, seconds.
    reconnect_backoff_s: float = 2.0

    # Per-unit setpoint source: unit_id -> cycle CSV path (real MPC horizon
    # preview, see reference/water_mpc/mpc_core.py's set_cycle) or a plain
    # float (constant target, today's behavior). Defaults to the project's
    # canonical demo: Unit1 tracks a step profile, Unit2 a ramp profile,
    # Unit3 holds a constant setpoint, so `docker compose up` reproduces
    # the demo with no extra configuration.
    unit_setpoint_sources: dict = field(default_factory=dict)


# Repo-root-relative, resolved against this file's parent's parent at call time.
DEFAULT_UNIT_CYCLES = {1: "cycles/step_response.csv", 2: "cycles/ramp_response.csv"}
DEFAULT_UNIT_CONSTANT_TARGET_M = 0.15


def load_config() -> Config:
    raw_endpoints = os.environ.get("PLC_ENDPOINTS", "")
    endpoints = _parse_endpoints(raw_endpoints)
    if not endpoints:
        # Development default: matches `python tools/fake_plc.py --units 3`.
        endpoints = [
            "opc.tcp://127.0.0.1:4840/",
            "opc.tcp://127.0.0.1:4841/",
            "opc.tcp://127.0.0.1:4842/",
        ]

    unit_setpoint_sources: dict = {}
    for i in range(1, len(endpoints) + 1):
        cycle_env = os.environ.get(f"DCS_UNIT{i}_CYCLE")
        target_env = os.environ.get(f"DCS_UNIT{i}_TARGET_M")
        if cycle_env:
            unit_setpoint_sources[i] = cycle_env
        elif target_env:
            unit_setpoint_sources[i] = float(target_env)
        elif i in DEFAULT_UNIT_CYCLES:
            unit_setpoint_sources[i] = DEFAULT_UNIT_CYCLES[i]
        else:
            unit_setpoint_sources[i] = DEFAULT_UNIT_CONSTANT_TARGET_M

    return Config(
        plc_endpoints=endpoints,
        dcs_server_endpoint=os.environ.get("DCS_SERVER_ENDPOINT", "opc.tcp://0.0.0.0:4900/"),
        cycle_s=float(os.environ.get("DCS_CYCLE_S", "1.0")),
        solve_budget_s=float(os.environ.get("DCS_SOLVE_BUDGET_S", "2.0")),
        heartbeat_stall_cycles=int(os.environ.get("DCS_HEARTBEAT_STALL_CYCLES", "5")),
        historian_db_path=os.environ.get("DCS_HISTORIAN_DB", "dcs_historian.sqlite3"),
        reconnect_backoff_s=float(os.environ.get("DCS_RECONNECT_BACKOFF_S", "2.0")),
        unit_setpoint_sources=unit_setpoint_sources,
    )
