"""One-unit fake PLC, used only by demos/ to get process-level isolation
between units that tools/fake_plc.py does not offer.

tools/fake_plc.py (frozen, owned outside hmi/demos/) runs all N units as
asyncio tasks inside a single process, started with `--units N`. That is
fine for routine hmi/ development but makes it impossible to kill "just
unit 2" the way `docker stop plc-2` will once plc/ ships one process per
unit (see task requirements). This script is the same idea as fake_plc.py
-- random-walk values over the tags.yaml address space, no physics -- but
takes an explicit --unit-id so demos can start three separate OS processes
and kill one of them independently, matching what docker-compose will do
once plc/ exists.

Usage:
    python demos/plc_stub.py --unit-id 2 --port 4841
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asyncua import Server, ua

from plantbus.contract import UNIT_TAGS
from plantbus.server import build_unit_nodes

UPDATE_PERIOD_S = 0.5

RANDOM_WALK_TAGS = {
    "Pump.CMD": (0.0, 100.0, 2.0),
    "Pump.FB": (0.0, 100.0, 2.0),
    "PID.OUT": (0.0, 100.0, 2.0),
    "Status.ScanTime_ms": (50.0, 150.0, 5.0),
}
# Level.PV is deliberately left out of the random walk: demo_a forces a step
# disturbance on it, which a genuine random walk would immediately wash out.
# Other demos that don't touch Level.PV can rely on its seeded constant
# value instead (see below).
#
# Level.PV is access:R in tags.yaml, so no OPC UA client can write it (the
# server enforces that even for this stub, same as the real plc/). A demo
# script that wants to force a disturbance has to ask this process to do it
# from inside its own event loop instead. It does that by dropping a file
# at DISTURBANCE_FILE containing the target level in meters; this loop
# polls for it once per update, applies it, and deletes the file, the same
# pattern plc/unit.py uses for its interlock reset file.


def _walk(value: float, lo: float, hi: float, step: float) -> float:
    value += random.uniform(-step, step)
    return max(lo, min(hi, value))


async def run(unit_id: int, port: int, disturbance_file: str):
    disturbance_path = Path(disturbance_file)
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{port}/")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    idx = await server.register_namespace("http://water-plant-dcs/demo-plc-stub")
    objects = server.get_objects_node()
    unit_object = await objects.add_object(idx, f"Unit{unit_id}")
    nodes = await build_unit_nodes(server, unit_object, UNIT_TAGS)

    await nodes["Level.HH"].write_value(0.18)
    await nodes["Level.LL"].write_value(0.01)
    await nodes["Level.PV"].write_value(0.10)
    await nodes["Level.SP"].write_value(0.10)
    await nodes["Pump.Running"].write_value(True)
    await nodes["PID.Mode"].write_value("CASCADE")
    await nodes["PID.SP"].write_value(0.10)
    await nodes["Interlock.Reason"].write_value("")

    heartbeat = 0
    print(f"[plc_stub] Unit{unit_id} serving on opc.tcp://0.0.0.0:{port}/", flush=True)

    async with server:
        while True:
            if disturbance_path.exists():
                target_level_m = float(disturbance_path.read_text().strip())
                await nodes["Level.PV"].write_value(target_level_m)
                disturbance_path.unlink()
                print(f"[plc_stub] Unit{unit_id}: applied disturbance, Level.PV -> {target_level_m} m", flush=True)

            for tag_name, (lo, hi, step) in RANDOM_WALK_TAGS.items():
                node = nodes[tag_name]
                current = await node.read_value()
                await node.write_value(_walk(current, lo, hi, step))

            heartbeat += 1
            await nodes["Status.Heartbeat"].write_value(ua.Variant(heartbeat, ua.VariantType.UInt32))
            await asyncio.sleep(UPDATE_PERIOD_S)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-id", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--disturbance-file", default=None,
        help="Path polled once per update for a one-shot Level.PV override (see module docstring). "
             "Defaults to ./demo_stub_unit<N>.disturbance",
    )
    args = parser.parse_args()
    disturbance_file = args.disturbance_file or f"./demo_stub_unit{args.unit_id}.disturbance"
    asyncio.run(run(args.unit_id, args.port, disturbance_file))


if __name__ == "__main__":
    main()
