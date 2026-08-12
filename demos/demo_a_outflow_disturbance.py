"""Demo A: outflow disturbance, APC on vs APC off.

Scenario: unit 1 is holding a level setpoint. At t=10s the outlet valve
opens further (Valve.CMD step, simulating a downstream demand increase),
draining the tank faster. Expected result: with APC on, the DCS should push
a compensating PID.SP change to bring Level.PV back toward its target; with
APC off, only the unit's local PID reacts, and since Valve.CMD is not fed
back into a level controller in this simulation, Level.PV should show a
larger, uncorrected deviation.

LIMITATION: dcs/ does not exist in this worktree yet (built in a parallel
worktree, see the task's shared constraints), so there is no APC to turn on
or off here. This script demonstrates the open-loop half of the comparison
only: it starts one PLC unit stub, applies the valve step, and prints the
Level.PV trend so you can see the uncontrolled response. Once dcs/ is
available, re-run this same disturbance with APC.Enabled=true vs false
(toggle via the HMI or POST /api/apc/enabled) and compare the two Level.PV
traces; APC on should show visibly smaller deviation and faster return to
setpoint than APC off.

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
    stub = subprocess.Popen(
        [sys.executable, str(ROOT / "demos" / "plc_stub.py"), "--unit-id", "1", "--port", str(UNIT_PORT)]
    )
    try:
        print(f"[demo_a] waiting for plc_stub on {ENDPOINT} ...")
        await asyncio.sleep(3)

        async with Client(url=ENDPOINT, timeout=5) as client:
            unit = await _find_unit_object(client, 1)
            from plantbus.client import read_all, resolve_unit_nodes
            from plantbus.contract import UNIT_TAGS

            nodes = await resolve_unit_nodes(unit, UNIT_TAGS)

            print(f"[demo_a] running {RUN_S}s, Valve.CMD step to 60% at t={DISTURBANCE_AT_S}s")
            start = time.monotonic()
            disturbed = False
            while time.monotonic() - start < RUN_S:
                elapsed = time.monotonic() - start
                if not disturbed and elapsed >= DISTURBANCE_AT_S:
                    await nodes["Valve.CMD"].write_value(60.0)
                    await nodes["Valve.FB"].write_value(60.0)
                    print(f"[demo_a] t={elapsed:5.1f}s  ** Valve.CMD stepped to 60% (outflow disturbance) **")
                    disturbed = True

                values = await read_all(nodes)
                print(
                    f"[demo_a] t={elapsed:5.1f}s  Level.PV={values['Level.PV']:.3f} m  "
                    f"Level.SP={values['Level.SP']:.3f} m  Valve.CMD={values['Valve.CMD']:.1f}%"
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


if __name__ == "__main__":
    asyncio.run(main())
