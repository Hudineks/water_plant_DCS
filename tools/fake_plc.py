"""Fake PLC: publishes the full tags.yaml address space for N units with
random-walk values. Not a simulator, no physics, no PID, no interlocks.

Purpose: let dcs/ and hmi/ development start immediately without waiting on
the real plc/ implementation. Both must work against this before they are
considered done, per SHARED CONSTRAINTS.

Usage:
    python tools/fake_plc.py --units 3 --base-port 4840
    # unit N listens on opc.tcp://0.0.0.0:<base-port + N - 1>/
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
    "Level.PV": (0.0, 4.0, 0.05),
    "Pump.CMD": (0.0, 100.0, 2.0),
    "Pump.FB": (0.0, 100.0, 2.0),
    "Valve.CMD": (0.0, 100.0, 2.0),
    "Valve.FB": (0.0, 100.0, 2.0),
    "PID.OUT": (0.0, 100.0, 2.0),
    "Status.ScanTime_ms": (50.0, 150.0, 5.0),
}


def _walk(value: float, lo: float, hi: float, step: float) -> float:
    value += random.uniform(-step, step)
    return max(lo, min(hi, value))


async def run_unit(unit_id: int, port: int):
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{port}/")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    uri = "http://water-plant-dcs/fake-plc"
    idx = await server.register_namespace(uri)

    objects = server.get_objects_node()
    unit_object = await objects.add_object(idx, f"Unit{unit_id}")

    nodes = await build_unit_nodes(server, unit_object, UNIT_TAGS)

    await nodes["Level.HH"].write_value(9.0)
    await nodes["Level.LL"].write_value(0.5)
    await nodes["Pump.Running"].write_value(True)
    await nodes["PID.Mode"].write_value("CASCADE")
    await nodes["PID.SP"].write_value(2.0)
    await nodes["Interlock.Reason"].write_value("")

    heartbeat = 0
    print(f"[fake_plc] Unit{unit_id} serving on opc.tcp://0.0.0.0:{port}/")

    async with server:
        while True:
            for tag_name, (lo, hi, step) in RANDOM_WALK_TAGS.items():
                node = nodes[tag_name]
                current = await node.read_value()
                await node.write_value(_walk(current, lo, hi, step))

            heartbeat += 1
            await nodes["Status.Heartbeat"].write_value(ua.Variant(heartbeat, ua.VariantType.UInt32))

            await asyncio.sleep(UPDATE_PERIOD_S)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--units", type=int, default=3)
    parser.add_argument("--base-port", type=int, default=4840)
    args = parser.parse_args()

    tasks = [
        run_unit(unit_id, args.base_port + unit_id - 1)
        for unit_id in range(1, args.units + 1)
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
