# plc/ — simulated PLC unit

One process = one tank unit (two tanks in series, one pump, local PID,
interlocks), exposed over OPC UA using the exact address space defined in
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

`model.py`: two tanks in series, matching the original Vodarna rig exactly
(same physical constants as `reference/water_mpc/mpc_core.py`: 50 mm tank
diameter, 4 mm outlet orifice, Cd=0.61). A pump fills the upstream tank
(`h1`, no sensor, never published over OPC UA); the upstream tank drains
by gravity through a fixed orifice into the downstream tank (`h2`, this is
`Level.PV`); the downstream tank drains by its own fixed orifice to the
sump. No valve anywhere, one actuator only:

```
dH1/dt = (Q_pump - K_out*sqrt(H1)) / F_area
dH2/dt = (K_out*sqrt(H1) - K_out*sqrt(H2)) / F_area
Q_pump = pump_max_flow_cm3s * (Pump.CMD / 100)
```

Internal state is centimeters (matching the reference model and the real
rig's own units); the OPC UA boundary (`Level.PV`) is meters, converted
only where that boundary is crossed. `H1` (the upstream tank) is tracked
internally but is not part of tags.yaml, since the real rig never exposes
it either — `dcs/` estimates it independently with its own EKF and
publishes that as a separate diagnostic (`Unit{n}/Diagnostics/H1_Estimated`
on the DCS's own OPC UA server, see `dcs/README.md`), which has no
relationship to this file's internal `h1_cm` beyond both approximating the
same physical quantity.

Engineering units, matching tags.yaml:

| Tag | Unit | Notes |
|---|---|---|
| Level.PV / SP / HH / LL | m | downstream tank level (h2), the measured/controlled one |
| Pump.CMD / FB | % | 0-100, no actuator lag modeled |
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
- **Dry-run** — pump commanded above 5% while level sits at or below 0.005 m
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
write is older than 30 seconds (`SP_STALE_TIMEOUT_S`, generous enough to
survive a real DCS's own startup latency, roughly 10-15s to build 3
do-mpc/casadi MPC controllers, without tripping AUTO before the DCS ever
gets a chance to write), the unit drops to AUTO on the last known good
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
| `LEVEL_HH` | `0.18` | HH interlock threshold, m |
| `LEVEL_LL` | `0.01` | LL interlock threshold, m |
| `UNIT_LOCAL_SP` | `0.10` | Initial AUTO-mode local setpoint, m |
| `PID_KP` / `PID_KI` / `PID_KD` | `600.0` / `10.0` / `100.0` | PID gains, tuned for the two-tank rig's much smaller, much faster-responding scale (see `plc/tests/test_scan_engine.py` for the empirical basis) |
| `PUMP_MAX_FLOW_CM3S` | `17.0` | Inflow at 100% pump, cm³/s, matches `reference/water_mpc/mpc_core.py`'s `U_MAX` |
| `INITIAL_LEVEL_M` | `0.05` | Starting level for both tanks, m |
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

Run with:

```
cd water_plant_DCS
python -m pytest plc/tests -v
```

Also `plc/tests/test_model.py`, covering the two-tank ODE itself in
isolation: zero-input drains to zero, levels never go negative, full-pump
steady state matches the Torricelli balance analytically, and the upstream
tank leads the downstream tank during a fill.

`plc/tests/test_scan_engine.py` covers `ScanEngine` directly (no OPC UA
needed to run these): PID settling after a setpoint step, HH interlock
latching and manual reset, and the CASCADE-to-AUTO fallback on a stale
`PID.SP`. Kept under `plc/tests/` rather
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
- No actuator dynamics (pump feedback equals command instantly, no lag, no
  stiction, no failure modes beyond the modeled interlocks).
- Manual interlock reset and MAN-mode pump override are implemented but not
  reachable from any tag in the current contract, see OPEN_QUESTIONS.md.
