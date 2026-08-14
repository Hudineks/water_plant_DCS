# Water Plant DCS

A simulated water treatment DCS/APC stack: PLC-level regulatory control, an
OPC UA supervisory MPC layer, and a browser-based operator panel, wired the
way these systems are actually structured in a real plant.

![Operator panel running the three preset setpoint cycles](demos/demo_e_setpoint_cycles.gif)

Three simulated PLC units, live in a browser at `http://localhost:8080/`
after `docker compose up`. Each unit tracks its own setpoint (step / ramp /
manual, switchable live from the CYCLE dropdown) -- Unit 1's flow visibly
ramps ahead of its step before the level even moves, because the MPC
previews the change across its solve horizon instead of reacting after the
fact. See ["Why the MPC actually matters here"](#why-the-mpc-actually-matters-here-preset-setpoint-cycles)
below for what's happening in that recording.

This is the industrial-control counterpart to the original Vodarna project
(a physical two-tank rig with a do-mpc controller): the same MPC core, now
driving simulated PLC units over OPC UA instead of talking to hardware
directly.

Each unit mirrors the rig's physics: a pump fills an upstream tank (no
sensor, EKF-estimated only), which drains by gravity into a downstream
tank (the one with the real sensor, `Level.PV`), which drains by its own
orifice to the sump. One actuator, no valve.

## The one rule that shapes everything else

**The MPC never touches an actuator. It writes setpoints.**

```
MPC (dcs/)  --writes-->  PID.SP  --converted by-->  plc/ flow-to-command  -->  Pump.CMD  -->  pump
```

`PID.SP` is a **flow** setpoint (cm³/s) the MPC writes straight through
every solve; the PLC's PID stage is a plain calculation rather than a
feedback loop, since nothing here models pump lag or delay for feedback
to correct.

If `dcs/` crashes or disconnects, the PLC's own watchdog notices the stale
setpoint and forces flow to zero rather than keep pumping on a stale
command.

## Architecture

```
                         ┌─────────────────────────────┐
                         │   hmi/  (FastAPI, :8080)     │
                         │  mimic, trends, per-unit cycle │
                         └───────────────┬───────────────┘
                       OPC UA client (units)  │  OPC UA client (dcs)
                 ┌─────────────────────────────┼─────────────────────────────┐
                 │                             │                             │
                 ▼                             ▼                             │
   ┌─────────────────────────┐   ┌─────────────────────────┐                 │
   │  plc-1 (asyncua SERVER) │   │  plc-2, plc-3 ...        │                 │
   │  two-tank ODE + PID +   │   │  same pattern            │                 │
   │  interlocks + heartbeat │   │                           │                │
   └────────────▲────────────┘   └────────────▲──────────────┘                │
                 │  OPC UA client (Level.PV, Status.Heartbeat)                │
                 │  OPC UA write (PID.SP flow setpoint, Level.SP target)     │
                 └─────────────────────────────┬───────────────────────────────
                                                │
                                   ┌────────────┴────────────┐
                                   │  dcs/  (asyncua CLIENT   │
                                   │  to units, SERVER of its │
                                   │  own for APC.* globals)  │
                                   │  nonlinear MPC + EKF     │
                                   │  per-unit heartbeat      │
                                   │  watchdog, SQLite log     │
                                   └───────────────────────────┘
```

Each PLC unit is its own OPC UA server. `dcs/` is a client to every unit
and, at the same time, runs its own small OPC UA server so the HMI can
read/write the global `APC.*` tags without a separate protocol. It also
publishes each unit's EKF-estimated upstream level as a read-only
diagnostic, since the real rig has no sensor there either.

## Why the MPC actually matters here: preset setpoint cycles

A flat, unchanging target isn't where an MPC earns its keep over a plain
PID -- handing it a real setpoint *trajectory* is, since it can anticipate
a change before it happens instead of reacting after the fact.

- **Unit 1** tracks a step ([`cycles/step_response.csv`](cycles/step_response.csv)):
  `PID.SP` starts ramping ahead of the step because the MPC previews it
  across its solve horizon.
- **Unit 2** tracks a ramp ([`cycles/ramp_response.csv`](cycles/ramp_response.csv)):
  `PID.SP` follows whatever flow the MPC judges will track it.
- **Unit 3** tracks a manually-entered setpoint, live-editable from the
  HMI at any time. The MPC still controls it, but a manual target isn't
  known in advance, so there is nothing to preview -- it only reacts once
  the operator actually changes it.

Each unit's mode is switchable live from the HMI's per-unit CYCLE
dropdown (visible in the recording above), not fixed at startup. See
[`demos/demo_e_setpoint_cycles.py`](demos/demo_e_setpoint_cycles.py) for
the script behind that recording.

## Contract-first build

The OPC UA address space is frozen in [`tags.yaml`](tags.yaml) before any
service code exists. `plantbus/` generates OPC UA server nodes and client
resolution from it, so `plc/`, `dcs/`, and `hmi/` all build against the
same layout instead of hand-copying tag names.

[`reference/water_mpc/`](reference/water_mpc) is the do-mpc + EKF
controller ported from the original Vodarna rig, unmodified in its model,
objective, constraints, and solver. `dcs/controller_wrapper.py` clips its
flow output to the actuator's physical bounds and writes it as `PID.SP`.

## Running it

```
docker compose up
```

Starts 3 PLC units, the DCS, and the HMI. Open `http://localhost:8080/`.
To run a single service standalone against `tools/fake_plc.py`, see that
service's own README: [`plc/README.md`](plc/README.md),
[`dcs/README.md`](dcs/README.md), [`hmi/README.md`](hmi/README.md).

## What this is not

- No real PLC runtime -- each unit is a plain Python process modeling
  tank physics, the PID/flow stage, and interlocks.
- No redundancy: one process per unit, no failover.
- No OPC UA certificates, encryption, or Alarms & Conditions; anonymous
  auth, matching most brownfield OT networks that rely on a firewall
  rather than the protocol layer for security.
- No Modbus -- OPC UA end to end.
- `docker compose up --scale plc=5` doesn't work with the fixed
  3-service compose file; [`demos/demo_d_scale_to_5.py`](demos/demo_d_scale_to_5.py)
  shows 5-unit scale-up against `tools/fake_plc.py` instead.

## Repository layout

```
tags.yaml               frozen OPC UA address space contract
plantbus/                tags.yaml -> OPC UA server nodes / client subscriptions
tools/fake_plc.py        dumb OPC UA server for standalone dev/testing
reference/water_mpc/     ported do-mpc + EKF controller core
cycles/                  setpoint-cycle CSV loader + the two demo cycle files
plc/                     PLC unit simulator (two-tank ODE, PID, interlocks, OPC UA server)
dcs/                     supervisory APC layer (MPC, watchdog, historian, OPC UA client+server)
hmi/                     operator panel (FastAPI + plain JS, OPC UA client)
demos/                   scenario scripts
docker-compose.yml       3 plc + 1 dcs + 1 hmi, one command to start everything
OPEN_QUESTIONS.md        contract gaps and integration issues found during the build
```
