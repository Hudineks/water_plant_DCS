"""Demo B: kill one PLC unit, confirm the HMI degrades gracefully.

Scenario: three units are running and the HMI is polling all three plus the
DCS. One unit's process is killed outright (the fake-PLC equivalent of
`docker stop plc-2`, since plc/ is not built in this worktree yet -- see
LIMITATION below). Expected result: the HMI marks unit 2 OFFLINE/COMM LOSS
in its alarms list and stops updating its trend, while units 1 and 3 keep
showing live values and the HMI process itself does not crash or stop
serving the websocket to the browser.

LIMITATION: docker-compose isn't runnable here (no `docker` in this dev
environment either, see hmi/README.md) and plc/ isn't built. This script
uses demos/plc_stub.py (one OS process per unit, unlike tools/fake_plc.py
which bundles all units into a single process) so that "kill unit 2" is a
real, isolated process kill, matching what `docker stop plc-2` will do once
plc/ exists.

Usage:
    python demos/demo_b_lost_unit.py
    (starts 3 plc_stub processes + the hmi server, kills unit 2 after 10s,
    polls /api/state for 20s so you can see units 1/3 stay alive and unit 2
    go offline, then shuts everything down)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HMI_PORT = 8091
UNIT_PORTS = {1: 4871, 2: 4872, 3: 4873}


def _http_get_json(url: str, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    procs = {}
    for unit_id, port in UNIT_PORTS.items():
        procs[f"unit{unit_id}"] = subprocess.Popen(
            [sys.executable, str(ROOT / "demos" / "plc_stub.py"), "--unit-id", str(unit_id), "--port", str(port)]
        )

    print("[demo_b] waiting for plc_stub processes to come up ...")
    time.sleep(3)

    endpoints = ",".join(f"opc.tcp://localhost:{p}/" for p in UNIT_PORTS.values())
    env = dict(**__import__("os").environ)
    env["PLC_ENDPOINTS"] = endpoints
    env["DCS_ENDPOINT"] = "opc.tcp://localhost:4999/"  # deliberately nothing there, DCS not built yet

    hmi = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hmi.main:app", "--host", "0.0.0.0", "--port", str(HMI_PORT)],
        cwd=str(ROOT),
        env=env,
    )

    try:
        print(f"[demo_b] waiting for hmi on http://localhost:{HMI_PORT} ...")
        time.sleep(5)

        for t in range(20):
            if t == 10:
                print("[demo_b] t=10s  ** killing unit 2's plc_stub process (docker stop plc-2 equivalent) **")
                procs["unit2"].terminate()
                procs["unit2"].wait(timeout=5)

            try:
                state = _http_get_json(f"http://localhost:{HMI_PORT}/api/state")
                summary = {
                    uid: ("ONLINE" if u["connected"] and u["alive"] else "OFFLINE")
                    for uid, u in state["units"].items()
                }
                print(f"[demo_b] t={t:2d}s  units={summary}")
            except Exception as exc:
                print(f"[demo_b] t={t:2d}s  hmi not reachable yet ({exc})")

            time.sleep(1)

        print("[demo_b] done. Expected: unit '2' flips to OFFLINE after t=10s while")
        print("[demo_b] '1' and '3' stay ONLINE the whole time, and /api/state kept")
        print("[demo_b] responding throughout (the hmi process never went down).")
    finally:
        hmi.terminate()
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        hmi.wait(timeout=5)
        for p in procs.values():
            if p.poll() is None:
                p.wait(timeout=5)


if __name__ == "__main__":
    main()
