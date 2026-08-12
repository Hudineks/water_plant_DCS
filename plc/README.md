# plc/ — simulated PLC unit

One process = one tank unit (tank + pump + valve + local PID + interlocks),
exposed over OPC UA using the exact address space defined in
`water_plant_DCS/tags.yaml`. Run several instances with different `UNIT_ID`
and `OPCUA_PORT` to simulate several units.

```
UNIT_ID=1 OPCUA_PORT=4840 python -m plc.unit
UNIT_ID=2 OPCUA_PORT=4841 python -m plc.unit
```

Run from the `water_plant_DCS/` directory (or with it on `PYTHONPATH`), the
module needs `plantbus/` and `tags.yaml` as siblings, same as `tools/fake_plc.py`.

## Scan structure

`unit.py` runs a fixed 100 ms wall-clock loop (`SCAN_PERIOD_S`), same shape
as a real PLC scan:

1. **Read inputs** — read `PID.SP` from OPC UA (this is the only input a DCS
   can write) and its write timestamp.
2. **Execute logic** — `scan_engine.ScanEngine.scan()`: watchdog check, mode
   resolution, interlock evaluation, PID compute, tank integration. This part
   has no OPC UA dependency and is what `plc/tests/test_scan_engine.py` drives
   directly.
3. **Write outputs** — every unit tag in `tags.yaml` is written back to its
   OPC UA node.
4. Sleep out the remainder of the 100 ms budget (`Status.ScanTime_ms` reports
   how long steps 1-3 actually took, it is sub-millisecond in this
   simulation).

The physics integration step uses `dt_sim = SCAN_PERIOD_S * TIME_SCALE`, so
`TIME_SCALE` speeds up how fast the simulated process moves without changing
the real scan cadence (the loop still runs every ~100 ms of wall time).

## Process model

`model.py`: single well-mixed tank, mass balance:

```
dLevel/dt = (Q_in - Q_out) / area
Q_in  = pump_max_flow * (Pump.CMD / 100)
Q_out = valve_cv * (Valve.CMD / 100) * sqrt(level)      # gravity outflow
```

`Valve.CMD` is a disturbance input in this simulation, held at a fixed
opening (`VALVE_CMD_PCT`, default 30%) — nothing in tags.yaml lets an
external client drive it, see OPEN_QUESTIONS.md.

Engineering units, matching tags.yaml:

| Tag | Unit | Notes |
|---|---|---|
| Level.PV / SP / HH / LL | m | tank level |
| Pump.CMD / FB | % | 0-100, no actuator lag modeled |
| Valve.CMD / FB | % | 0-100, fixed disturbance |
| PID.SP | m | cascade setpoint from the DCS |
| PID.OUT | % | pre-limit PID output (limits are 0-100 in this simulation, so it equals Pump.CMD) |
| Status.ScanTime_ms | ms | measured duration of step 2-3 above |

## Regulatory layer (PID)

`pid.py`: positional PID, derivative on measurement (no setpoint kick),
output clamped to 0-100%, anti-windup by conditional integration (the
integral only accumulates while the output is not saturated, or while the
error is already pulling it back inside the limits).

Modes:

- **CASCADE** — PID follows `PID.SP` as written by the DCS.
- **AUTO** — PID follows an internally held local setpoint.
- **MAN** — PID is bypassed, `Pump.CMD` is held at whatever it last was.

See OPEN_QUESTIONS.md for why mode selection and the MAN pump command are
internal-only in this build: tags.yaml does not expose a writable mode-select
or manual-command tag.

## Interlocks

`interlocks.py`, latched trips (stay tripped until reset, on purpose):

- **HH** — `Level.PV >= Level.HH`.
- **LL** — `Level.PV <= Level.LL`.
- **Dry-run** — pump commanded above 5% while level sits at or below 0.10 m
  for 20 consecutive scans (~2 s at 100 ms). This only fires in
  configurations where `Level.LL` is at or below that floor, since the LL
  trip fires first otherwise — documented as a known limitation, not a bug.

On a trip: `Pump.CMD` forced to 0, `PID.OUT` forced to 0, `Interlock.Trip`
set true, `Interlock.Reason` set to the cause, PID integral cleared so it
does not bump when control resumes.

**Manual reset**: tags.yaml has no reset tag (see OPEN_QUESTIONS.md). This
build uses a reset file as a stand-in for a physical reset button: if a file
at `RESET_FILE` (default `./unit<N>.reset`) exists at the start of a scan,
the trip is cleared and the file is deleted. Reset from a shell:

```
touch unit1.reset
```

## Watchdog

`Status.Heartbeat` increments every scan, free-running `uint32`. A dead unit
is one whose heartbeat stops advancing.

Separately, the PID.SP cascade watchdog: each scan reads the OPC UA
timestamp of the last write to `PID.SP`. If the unit is in CASCADE and that
write is older than 5 seconds, the unit drops to AUTO on the last known good
setpoint and logs a warning. It stays in AUTO until the process is restarted
(there is no writable mode-select tag to put it back into CASCADE, see
OPEN_QUESTIONS.md) — a real DCS resuming its writes will find the unit
already fell back rather than silently running open-loop.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `UNIT_ID` | `1` | Used in the OPC UA object name `UnitN` and the default reset-file name |
| `OPCUA_PORT` | `4840` | OPC UA server TCP port |
| `TIME_SCALE` | `1.0` | Simulated-time multiplier, see "Scan structure" above |
| `LEVEL_HH` | `4.0` | HH interlock threshold, m |
| `LEVEL_LL` | `0.3` | LL interlock threshold, m |
| `UNIT_LOCAL_SP` | `2.0` | Initial AUTO-mode local setpoint, m |
| `VALVE_CMD_PCT` | `30.0` | Fixed valve opening (disturbance), % |
| `PID_KP` / `PID_KI` / `PID_KD` | `40.0` / `5.0` / `0.0` | PID gains |
| `TANK_AREA_M2` | `2.0` | Tank cross-section, m² |
| `PUMP_MAX_FLOW_M3S` | `0.05` | Inflow at 100% pump, m³/s |
| `VALVE_CV` | `0.03` | Outflow coefficient at 100% valve opening |
| `INITIAL_LEVEL_M` | `1.5` | Starting tank level, m |
| `UNIT_INITIAL_MODE` | `CASCADE` | AUTO / MAN / CASCADE at startup |
| `RESET_FILE` | `./unit<UNIT_ID>.reset` | Path polled each scan for a manual interlock reset |

## Running standalone

```
cd water_plant_DCS
pip install asyncua PyYAML
UNIT_ID=1 OPCUA_PORT=4840 TIME_SCALE=1.0 python -m plc.unit
```

Connect with any OPC UA client at `opc.tcp://<host>:4840/`, anonymous auth,
no security policy. Node layout: `Objects/Unit1/Level/PV`,
`Objects/Unit1/PID/SP`, etc, one object per tag group, matching
`plantbus.server.build_unit_nodes`.

## Tests

`plc/tests/test_scan_engine.py`, run with:

```
cd water_plant_DCS
python -m pytest plc/tests -v
```

Covers `ScanEngine` directly (no OPC UA needed to run these): PID settling
after a setpoint step, HH interlock latching and manual reset, and the
CASCADE-to-AUTO fallback on a stale `PID.SP`. Kept under `plc/tests/` rather
than a top-level `tests/plc/` because the engine has no dependency outside
`plc/` and this keeps the test next to the code it exercises, consistent
with `tools/` and `plantbus/` each owning their own concerns.

The OPC UA server wiring in `unit.py` was verified manually with a live
`asyncua.Client` (not covered by an automated OPC UA test in this v1) — see
the PR/session notes for the exact commands and observed output.

## Known simplifications (v1, intentional)

- No real PLC runtime (no ladder/ST/IL, no scan-cycle watchdog fault
  handling beyond the SP watchdog described above).
- No redundancy (single process, single OPC UA endpoint per unit).
- No OPC UA certificates or encryption, anonymous auth, `NoSecurity` policy,
  matching `tools/fake_plc.py` and the SHARED CONSTRAINTS for this project.
- No OPC UA Alarms & Conditions, `Interlock.Trip`/`Reason` are plain
  variables, not AlarmConditions.
- No Modbus or any fieldbus, this unit is the "field" in this simulation.
- No actuator dynamics (pump/valve feedback equals command instantly, no
  lag, no stiction, no failure modes beyond the modeled interlocks).
- Manual interlock reset and MAN-mode pump override are implemented but not
  reachable from any tag in the current contract, see OPEN_QUESTIONS.md.
