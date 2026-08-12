"""Demo A: outflow disturbance, APC on vs APC off.

Scenario: unit 1 is holding a level setpoint. At t=10s an abrupt downstream
demand spike drains the tank faster (modeled here as a direct step on
Level.PV, since the real rig has no valve to script a disturbance through,
see tags.yaml -- the physical model only has a pump and two fixed gravity
orifices). Expected result: with APC on, the DCS should push a compensating
PID.SP change to bring Level.PV back toward its target; with APC off, only
the unit's local PID reacts, and Level.PV should show a larger, slower
return to setpoint.

LIMITATION: this script deliberately uses demos/plc_stub.py, which has no
PID/interlock logic of its own (pure random walk plus the scripted
disturbance write), not the real plc/unit.py, so there is nothing here for
an APC to correct. It demonstrates the open-loop half of the comparison
only. For the real controlled-vs-uncontrolled comparison, run the actual
stack (see demos/demo_e_setpoint_cycles.py for the pattern: real
plc/unit.py processes plus dcs/main.py) with APC.Enabled toggled true vs
false across two runs of the same disturbance, and compare the two
Level.PV traces; APC on should show visibly smaller deviation and faster
return to setpoint than APC off.

Usage:
    python demos/demo_a_outflow_disturbance.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asyncua import Client, ua

ROOT = Path(__file__).resolve().parent.parent
UNIT_PORT = 4870
ENDPOINT = f"opc.tcp://localhost:{UNIT_PORT}/"
DISTURBANCE_FILE = ROOT / "demo_a_unit1.disturbance"
RUN_S = 40
DISTURBANCE_AT_S = 10


async def _find_unit_object(client, unit_id):
    objects = client.get_objects_node()
    for child in await objects.get_children():
        bn = await child.read_browse_name()
        if bn.Name == f"Unit{unit_id}":
            return child
    raise RuntimeError("unit object not found")


async def main():
    DISTURBANCE_FILE.unlink(missing_ok=True)
    stub = subprocess.Popen(
        [
            sys.executable, str(ROOT / "demos" / "plc_stub.py"),
            "--unit-id", "1", "--port", str(UNIT_PORT),
            "--disturbance-file", str(DISTURBANCE_FILE),
        ]
    )
    try:
        print(f"[demo_a] waiting for plc_stub on {ENDPOINT} ...")
        await asyncio.sleep(10)

        async with Client(url=ENDPOINT, timeout=10) as client:
            unit = await _find_unit_object(client, 1)
            from plantbus.client import read_all, resolve_unit_nodes
            from plantbus.contract import UNIT_TAGS

            nodes = await resolve_unit_nodes(unit, UNIT_TAGS)

            print(f"[demo_a] running {RUN_S}s, Level.PV step disturbance (downstream demand spike) at t={DISTURBANCE_AT_S}s")
            start = time.monotonic()
            disturbed = False
            while time.monotonic() - start < RUN_S:
                elapsed = time.monotonic() - start
                if not disturbed and elapsed >= DISTURBANCE_AT_S:
                    DISTURBANCE_FILE.write_text("0.05")
                    print(f"[demo_a] t={elapsed:5.1f}s  ** disturbance requested: Level.PV -> 0.05 m (downstream demand spike) **")
                    disturbed = True

                values = await read_all(nodes)
                print(
                    f"[demo_a] t={elapsed:5.1f}s  Level.PV={values['Level.PV']:.3f} m  "
                    f"Level.SP={values['Level.SP']:.3f} m"
                )
                await asyncio.sleep(1)

        print("[demo_a] done. Expected: Level.PV drifts away from Level.SP after the")
        print("[demo_a] step and does not correct itself, because this stub has no")
        print("[demo_a] closed-loop level control and no APC is running. Re-run against")
        print("[demo_a] the real plc/+dcs/ stack with APC.Enabled toggled to see the")
        print("[demo_a] controlled-vs-uncontrolled comparison this demo is named for.")
    finally:
        stub.terminate()
        stub.wait(timeout=5)
        DISTURBANCE_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
