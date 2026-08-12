"""Demo E: per-unit preset setpoint cycles, real MPC horizon lookahead.

Scenario: three real plc/unit.py processes plus a real dcs/main.py process,
using dcs/config.py's default per-unit setpoint assignment (no extra env
vars needed): Unit 1 tracks cycles/step_response.csv, Unit 2 tracks
cycles/ramp_response.csv, Unit 3 holds a plain constant setpoint. This is
the demo meant to be screen-recorded: it is the one that shows the actual
point of using an MPC instead of a plain PID for the DCS layer, since
Unit 1 and Unit 2's controllers get a real preview of the setpoint
trajectory across their solve horizon (see
reference/water_mpc/mpc_core.py's set_cycle), not just today's error.

Runtime note: both example cycles have a 4 minute period (step_response.csv:
flat at 5 cm for 2 min, step to 10 cm for ~1 min, back to 5 cm; ramp_response.csv:
0 cm -> 15 cm over 2 min, back down over the next 2 min), so this script's
default RUN_S=300s (5 minutes) comfortably covers a full period of both,
including the step transition around t=2min and the anticipatory flow
rise the MPC starts commanding (via PID.SP, cm3/s) well before that (its
horizon is 40s, so watch it lead the step by about that much) -- `Level.SP`
itself still steps instantly, since it holds the DCS's actual instantaneous
target; it's the commanded flow that ramps in early. Raise RUN_S to watch
more than one period,
or set TIME_SCALE below 1.0's default to change the physical plant's own
settling speed -- but note that speeding the plant up more than the cycle
relative to real time makes MPC's lookahead advantage less visible, since
a fast-settling plant does not lag behind a setpoint change even without
preview.

Usage:
    python demos/demo_e_setpoint_cycles.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asyncua import Client

ROOT = Path(__file__).resolve().parent.parent
PLC_PORTS = {1: 4890, 2: 4891, 3: 4892}
DCS_ENDPOINT = "opc.tcp://localhost:4899/"
RUN_S = int(os.environ.get("DEMO_E_RUN_S", "300"))
POLL_PERIOD_S = 5


async def _find_child(client, label):
    for child in await client.get_objects_node().get_children():
        bn = await child.read_browse_name()
        if bn.Name == label:
            return child
    raise RuntimeError(f"{label} not found")


async def _enable_apc():
    async with Client(url=DCS_ENDPOINT, timeout=5) as client:
        idx = 2
        global_obj = await _find_child(client, "Global")
        apc = await global_obj.get_child(f"{idx}:APC")
        await (await apc.get_child(f"{idx}:Enabled")).write_value(True)
        print("[demo_e] APC.Enabled -> true")


async def _poll_once():
    rows = []
    async with Client(url=f"opc.tcp://localhost:{PLC_PORTS[1]}/", timeout=5) as c1, \
               Client(url=f"opc.tcp://localhost:{PLC_PORTS[2]}/", timeout=5) as c2, \
               Client(url=f"opc.tcp://localhost:{PLC_PORTS[3]}/", timeout=5) as c3:
        idx = 2
        for label, client in (("Unit1(step)", c1), ("Unit2(ramp)", c2), ("Unit3(const)", c3)):
            unit = await _find_child(client, label.split("(")[0])
            level = await unit.get_child(f"{idx}:Level")
            pv = await (await level.get_child(f"{idx}:PV")).read_value()
            sp = await (await level.get_child(f"{idx}:SP")).read_value()
            rows.append(f"{label:14s} PV={pv:.4f} m  SP={sp:.4f} m")
    return rows


def main():
    plc_procs = {}
    for unit_id, port in PLC_PORTS.items():
        env = dict(**os.environ)
        env["UNIT_ID"] = str(unit_id)
        env["OPCUA_PORT"] = str(port)
        plc_procs[unit_id] = subprocess.Popen(
            [sys.executable, "-m", "plc.unit"], cwd=str(ROOT), env=env,
        )

    print("[demo_e] waiting for plc units to come up ...")
    time.sleep(4)

    dcs_env = dict(**os.environ)
    dcs_env["PLC_ENDPOINTS"] = ",".join(f"opc.tcp://localhost:{p}/" for p in PLC_PORTS.values())
    dcs_env["DCS_SERVER_ENDPOINT"] = DCS_ENDPOINT
    dcs_proc = subprocess.Popen(
        [sys.executable, "-m", "dcs.main"], cwd=str(ROOT), env=dcs_env,
    )

    try:
        print("[demo_e] waiting for dcs to come up (building 3 MPC controllers takes a few seconds) ...")
        time.sleep(15)

        asyncio.run(_enable_apc())

        print(f"[demo_e] running {RUN_S}s. Watch Unit1 hold flat then step, Unit2 ramp, Unit3 stay constant.")
        start = time.monotonic()
        while time.monotonic() - start < RUN_S:
            elapsed = time.monotonic() - start
            try:
                rows = asyncio.run(_poll_once())
                print(f"[demo_e] t={elapsed:6.1f}s  " + "  |  ".join(rows))
            except Exception as exc:
                print(f"[demo_e] t={elapsed:6.1f}s  not ready yet ({exc})")
            time.sleep(POLL_PERIOD_S)

        print("[demo_e] done. Expected: Unit1's PID.SP (a flow, cm3/s) starts rising")
        print("[demo_e] well before t=120s (the step in Level.SP from 0.05 to 0.10 m),")
        print("[demo_e] because the MPC previewed the step across its ~40s solve")
        print("[demo_e] horizon (see reference/water_mpc/mpc_core.py's set_cycle) ahead")
        print("[demo_e] of a plain reactive controller. Unit2's Level.SP should move")
        print("[demo_e] smoothly along the 0->0.15m->0 ramp, with PID.SP tracking the")
        print("[demo_e] flow needed to follow it. Unit3's Level.SP should hold its")
        print("[demo_e] manual target.")
    finally:
        dcs_proc.terminate()
        for p in plc_procs.values():
            p.terminate()
        dcs_proc.wait(timeout=5)
        for p in plc_procs.values():
            p.wait(timeout=5)


if __name__ == "__main__":
    main()
