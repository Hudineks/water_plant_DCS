# water_plant_DCS

A simulated water treatment DCS/APC stack: PLC-level regulatory control, an
OPC UA supervisory MPC layer, and a browser-based operator panel, wired the
way these systems are actually structured in a real plant.

This is the industrial-control counterpart to the original Vodarna project
(a physical single-tank rig with a do-mpc controller). Here the same MPC
core is ported into a proper cascade architecture, communicating over OPC
UA with simulated PLC units instead of talking to hardware or a GUI thread
directly.

## The one rule that shapes everything else

**The MPC never touches an actuator. It writes setpoints.**

In a real plant, an APC layer sits above the DCS and writes `PID.SP` into
the regulatory control loops. It never reaches down and moves a valve or a
pump directly. That separation is what this project demonstrates:

```
MPC (dcs/)  --writes-->  PID.SP  --tracked by-->  local PID (plc/)  -->  Pump.CMD  -->  pump
```

If the DCS process crashes, dies, or gets disconnected, each PLC unit keeps
its local PID loop running on the last known setpoint (or falls back to its
own local setpoint after a watchdog timeout). The plant does not stop
because a Windows box next to it went down.

## Architecture

```
                         ┌─────────────────────────────┐
                         │   hmi/  (FastAPI, :8080)     │
                         │   mimic, trends, APC toggle  │
                         └───────────────┬───────────────┘
                       OPC UA client (units)  │  OPC UA client (dcs)
                 ┌─────────────────────────────┼─────────────────────────────┐
                 │                             │                             │
                 ▼                             ▼                             │
   ┌─────────────────────────┐   ┌─────────────────────────┐                 │
   │  plc-1 (asyncua SERVER) │   │  plc-2, plc-3 ...        │                 │
   │  tank ODE + PID +       │   │  same pattern            │                 │
   │  interlocks + heartbeat │   │                           │                │
   └────────────▲────────────┘   └────────────▲──────────────┘                │
                 │  OPC UA client (Level.PV, Status.Heartbeat)                │
                 │  OPC UA write (PID.SP only)                                │
                 └─────────────────────────────┬───────────────────────────────
                                                │
                                   ┌────────────┴────────────┐
                                   │  dcs/  (asyncua CLIENT   │
                                   │  to units, SERVER of its │
                                   │  own for APC.* globals)  │
                                   │  linear MPC + EKF        │
                                   │  per-unit heartbeat      │
                                   │  watchdog, SQLite log     │
                                   └───────────────────────────┘
```

Each PLC unit is its own OPC UA server (one process per tank). `dcs/` is an
OPC UA client to every unit and, at the same time, runs its own small OPC UA
server so the HMI (or any other client) can read `APC.SolveTime_ms` /
`APC.Status` and write `APC.Enabled` over the same protocol, without a
separate side channel. `hmi/` is a plain OPC UA client to both.

## Contract-first build

The address space is frozen in [`tags.yaml`](tags.yaml) before any service
code exists. `plantbus/` turns that file into OPC UA server nodes
(`plantbus/server.py`) and client-side node resolution
(`plantbus/client.py`), so `plc/`, `dcs/`, and `hmi/` all build against the
same generated layout instead of hand-copying tag names. [`tools/fake_plc.py`](tools/fake_plc.py)
is a deliberately dumb OPC UA server that publishes the whole contract with
random-walk values, so `dcs/` and `hmi/` could be built and tested before
`plc/` existed.

[`reference/water_mpc/`](reference/water_mpc) holds the do-mpc + EKF
controller ported from the original Vodarna rig, unmodified in its model,
objective, constraints, and solver settings. `dcs/controller_wrapper.py`
adapts its interface to the cascade (see [`dcs/README.md`](dcs/README.md)
for exactly how a predicted level trajectory becomes `PID.SP`).

## Running it

```
docker compose up
```

Starts 3 PLC units, the DCS, and the HMI on one network. Open
`http://localhost:8080/` for the operator panel.

To run any single service standalone against `tools/fake_plc.py` instead of
the real `plc/`, see that service's own README:
[`plc/README.md`](plc/README.md), [`dcs/README.md`](dcs/README.md),
[`hmi/README.md`](hmi/README.md).

Scenario scripts are in [`demos/`](demos/README.md): an outflow disturbance
with APC on vs off, a killed unit degrading gracefully, a large setpoint
change against a level bound, and scaling to 5 units.

## What v1 deliberately is not

These are conscious simplifications, not gaps hidden as complete:

- No real PLC runtime (no OpenPLC, no Codesys). Each unit is a plain Python
  process modeling a tank, a PID, and interlocks.
- No redundancy. One PLC process per unit, one DCS process, no failover.
- No OPC UA certificates or encryption. Anonymous auth, `NoSecurity` policy,
  same as most brownfield OT networks running behind a firewall rather than
  relying on the protocol layer for security.
- No OPC UA Alarms & Conditions. Alarms are plain boolean/string tags
  (`Interlock.Trip`, `Interlock.Reason`), polled by the HMI, not the A&C
  subsystem.
- No Modbus. OPC UA end to end.
- `docker compose up --scale plc=5` does not work out of the box with the
  current fixed 3-service compose file (every replica would claim the same
  `UNIT_ID`). See `OPEN_QUESTIONS.md` for what scale-friendly handling would
  need; `demos/demo_d_scale_to_5.py` demonstrates 5-unit scale-up against
  `tools/fake_plc.py`, which does not have this limitation.
- The ported MPC's physical envelope (a small lab rig, roughly 0-0.2 m) does
  not match the plant-scale tank model in `plc/model.py`. `dcs/` clips
  `PID.SP` to the controller's own valid range rather than rescaling the
  ported physics. See `OPEN_QUESTIONS.md` for the full account.

`OPEN_QUESTIONS.md` has the complete list of contract gaps and integration
issues found while building this, including a couple of genuine bugs (an
OPC UA type mismatch in `tools/fake_plc.py`, a node-path mismatch between
`dcs/` and `hmi/` that only showed up once both were run together) and how
each was resolved.

## Repository layout

```
tags.yaml              frozen OPC UA address space contract
plantbus/               tags.yaml -> OPC UA server nodes / client subscriptions
tools/fake_plc.py       dumb OPC UA server for parallel development
reference/water_mpc/    ported do-mpc + EKF controller core
plc/                    PLC unit simulator (tank ODE, PID, interlocks, OPC UA server)
dcs/                    supervisory APC layer (MPC, watchdog, historian, OPC UA client+server)
hmi/                    operator panel (FastAPI + plain JS, OPC UA client)
demos/                  scenario scripts
docker-compose.yml      3 plc + 1 dcs + 1 hmi, one command to start everything
OPEN_QUESTIONS.md       contract gaps and integration issues found during the build
```
