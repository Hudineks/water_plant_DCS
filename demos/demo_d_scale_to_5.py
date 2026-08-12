"""Demo D: scale to 5 units.

Scenario: the plant grows from 3 units to 5. Expected result: the HMI shows
5 unit cards, all connected, with no code change beyond the PLC_ENDPOINTS
env var listing 5 endpoints instead of 3 -- the HMI has no hardcoded unit
count anywhere (see hmi/main.py, hmi/opcua_bridge.py, hmi/static/app.js:
all three build the unit list from however many endpoints/keys are present).

This is the part of "scaling to 5 units" this worktree can actually
demonstrate: 5 independent plc_stub processes + the hmi server, started
directly (not via docker-compose, since plc/'s Dockerfile does not exist
here and docker itself is not available in this dev environment, see
hmi/README.md). See docker-compose.yml's header comment and
OPEN_QUESTIONS.md for why `docker compose up --scale plc=5` specifically is
NOT feasible with the compose file as structured (fixed plc-1/plc-2/plc-3
service names, not a template), and what would need to change.

Usage:
    python demos/demo_d_scale_to_5.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HMI_PORT = 8093
BASE_PORT = 4890
N_UNITS = 5


def _http_get_json(url: str, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    procs = []
    for i in range(1, N_UNITS + 1):
        port = BASE_PORT + i - 1
        procs.append(
            subprocess.Popen(
                [sys.executable, str(ROOT / "demos" / "plc_stub.py"), "--unit-id", str(i), "--port", str(port)]
            )
        )

    print(f"[demo_d] waiting for {N_UNITS} plc_stub processes to come up ...")
    time.sleep(3)

    endpoints = ",".join(f"opc.tcp://localhost:{BASE_PORT + i - 1}/" for i in range(1, N_UNITS + 1))
    env = dict(**os.environ)
    env["PLC_ENDPOINTS"] = endpoints
    env["DCS_ENDPOINT"] = "opc.tcp://localhost:4997/"

    hmi = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hmi.main:app", "--host", "0.0.0.0", "--port", str(HMI_PORT)],
        cwd=str(ROOT),
        env=env,
    )

    try:
        print(f"[demo_d] waiting for hmi on http://localhost:{HMI_PORT} ...")
        time.sleep(5)

        state = _http_get_json(f"http://localhost:{HMI_PORT}/api/state")
        print(f"[demo_d] hmi reports {len(state['units'])} unit(s):")
        for uid, u in sorted(state["units"].items(), key=lambda kv: int(kv[0])):
            status = "ONLINE" if u["connected"] and u["alive"] else "OFFLINE"
            pv = u["values"].get("Level.PV")
            print(f"[demo_d]   unit {uid}: {status}  Level.PV={pv}")

        ok = len(state["units"]) == N_UNITS and all(
            u["connected"] and u["alive"] for u in state["units"].values()
        )
        print(f"[demo_d] result: {'PASS' if ok else 'FAIL'} -- all {N_UNITS} units online with no hmi code change")
    finally:
        hmi.terminate()
        for p in procs:
            p.terminate()
        hmi.wait(timeout=5)
        for p in procs:
            p.wait(timeout=5)


if __name__ == "__main__":
    main()
