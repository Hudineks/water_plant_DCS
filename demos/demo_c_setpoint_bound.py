"""Demo C: large setpoint change against a level bound.

Scenario: unit 1's Level.HH (high-high interlock threshold) is 4.0 m. The
operator enters a manual setpoint well above that (6.0 m) through the same
HMI endpoint the panel's "MANUAL SP" field uses (POST
/api/units/1/setpoint). Expected result on the real stack: the PLC's PID
follows PID.SP, level rises, and the interlock trips (Interlock.Trip=true,
Interlock.Reason="HH level") before level reaches the unsafe setpoint,
latching the unit safe rather than following the operator's number blindly.
Level.SP (the *effective* setpoint the PID is following) should therefore
diverge from PID.SP (what the operator asked for), because the PLC clamps
or trips instead of obeying literally -- that gap is the point of the demo.

LIMITATION: demos/plc_stub.py (used here since plc/ is not built in this
worktree) has no PID and no interlock logic, it only random-walks Level.PV
regardless of PID.SP. So this script can only demonstrate the HMI side of
the story: the write goes through the RW-access PID.SP tag exactly as
tags.yaml defines it, and the HMI does not itself apply any bound (it is
not its job to, per the SHARED CONSTRAINTS: interlocks are PLC-level). The
actual clamp/trip behavior can only be observed once plc/'s PID and
Interlock logic exist; re-run this script against that stack and watch
Interlock.Trip flip to true instead of Level.PV following the setpoint
past Level.HH.

Usage:
    python demos/demo_c_setpoint_bound.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_PORT = 4880
HMI_PORT = 8092


def _http_post_json(url: str, payload: dict, timeout=3):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _http_get_json(url: str, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    stub = subprocess.Popen(
        [sys.executable, str(ROOT / "demos" / "plc_stub.py"), "--unit-id", "1", "--port", str(UNIT_PORT)]
    )

    env = dict(**os.environ)
    env["PLC_ENDPOINTS"] = f"opc.tcp://localhost:{UNIT_PORT}/"
    env["DCS_ENDPOINT"] = "opc.tcp://localhost:4998/"

    hmi = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hmi.main:app", "--host", "0.0.0.0", "--port", str(HMI_PORT)],
        cwd=str(ROOT),
        env=env,
    )

    try:
        print("[demo_c] waiting for plc_stub and hmi to come up ...")
        time.sleep(6)

        before = _http_get_json(f"http://localhost:{HMI_PORT}/api/state")
        hh = before["units"]["1"]["values"].get("Level.HH")
        print(f"[demo_c] unit 1 Level.HH interlock threshold = {hh} m")

        target = 6.0
        print(f"[demo_c] writing PID.SP={target} m through the HMI (well above HH={hh} m)")
        result = _http_post_json(f"http://localhost:{HMI_PORT}/api/units/1/setpoint", {"value": target})
        print(f"[demo_c] write result: {result}")

        time.sleep(2)
        after = _http_get_json(f"http://localhost:{HMI_PORT}/api/state")
        v = after["units"]["1"]["values"]
        print(f"[demo_c] PID.SP now reads back as {v.get('PID.SP')} m (write accepted, as tags.yaml marks it RW)")
        print(f"[demo_c] Level.PV={v.get('Level.PV')} m, Interlock.Trip={v.get('Interlock.Trip')}")
        print("[demo_c] plc_stub has no PID/interlock, so Interlock.Trip stays false here.")
        print("[demo_c] Expected on the real stack: Interlock.Trip -> true, Interlock.Reason='HH level',")
        print("[demo_c] and Level.SP (effective) should not track PID.SP past the HH bound.")
    finally:
        hmi.terminate()
        stub.terminate()
        hmi.wait(timeout=5)
        stub.wait(timeout=5)


if __name__ == "__main__":
    main()
