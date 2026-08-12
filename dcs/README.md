# dcs/ — supervisory APC layer

Writes only `Unit*.PID.SP` (a level target in meters) into the PLC's local
PID loops. Never writes `Pump.CMD` or `Valve.CMD`. Cascade:
`MPC (dcs/) -> PID.SP -> PLC PID loop -> Pump.CMD -> pump`.

## OPC UA topology

```
                 opc.tcp://<plc-host-N>:<port>/     (N PLC servers, plc/ or
   dcs/  ------------------------------------->      tools/fake_plc.py)
 (client)         reads: Level.PV, Status.Heartbeat (subscribed),
                          full unit tag set (periodic, for the historian)
                  writes: PID.SP only

                 opc.tcp://0.0.0.0:4900/            (dcs/'s own server)
   hmi/  ------------------------------------->      dcs/
 (client)         reads: APC.SolveTime_ms, APC.Status
                  writes: APC.Enabled
```

- `dcs/` is an OPC UA **client** of every PLC unit (`opc_client.py`,
  `UnitClient`), one connection per endpoint in `PLC_ENDPOINTS`, using
  `plantbus.client.resolve_unit_nodes`/`read_all` (frozen, not modified) to
  browse and read the unit's address space.
- `dcs/` is also its own OPC UA **server** (`global_server.py`,
  `GlobalServer`), exposing the `global` tag group from `tags.yaml`
  (`APC.Enabled`, `APC.SolveTime_ms`, `APC.Status`) built with
  `plantbus.server.build_unit_nodes` (frozen, reused as-is — it only cares
  about the tag list's dotted-prefix grouping, which works the same for the
  global tags). This is the choice tags.yaml's comment ("live on the DCS's
  own server-side status") points at directly: rather than adding a second
  protocol (REST/websocket) for three tags, the HMI talks OPC UA to the DCS
  the same way it would talk to a PLC unit. `hmi/` should connect to
  `DCS_SERVER_ENDPOINT` (default `opc.tcp://0.0.0.0:4900/`), browse to
  `Global/APC/Enabled`, `Global/APC/SolveTime_ms`, `Global/APC/Status`.
- No third channel exists or is needed. `dcs/` never listens on anything but
  that one OPC UA server endpoint.

## Mapping from the MPC to PID.SP

`reference/water_mpc/mpc_core.py`'s `WaterTankController.step()` returns
`MPCResult.flow_cm3s`, a flow-actuator command. That value is **not used at
all** by `dcs/`: writing it anywhere would break the cascade rule.

Instead (`dcs/controller_wrapper.py`, `UnitController._extract_predicted_sp_m`):
after every `step()` call, do-mpc has the freshly re-solved predicted state
trajectory for the whole horizon in `controller.mpc.data.prediction(('_x',
'h2'))`. Index 1 along that array (one `t_step` = 1 s ahead, matching the
DCS's own 1 s control cycle) is the model's own prediction of where the
controlled level should be after the next second, given the input sequence
it just optimized. That value (cm -> m) is written as the next `PID.SP`.
This makes `PID.SP` move along the path the MPC computed, one cycle-sized
step at a time, instead of jumping straight to the operator's target, while
never touching an actuator tag.

`PID.SP` is additionally clipped in `_clip_to_interlock_bounds` to
`[MODEL_LEVEL_MIN_M, MODEL_LEVEL_MAX_M]` (0 to `H2_MAX/100`, i.e. 0-0.2 m),
the ported model's own valid state range, tightened further against the
unit's real `Level.LL`/`Level.HH` only when those fall inside that range.
See `OPEN_QUESTIONS.md` for why: the reference model is tuned for a small
lab rig (0-20 cm), `tools/fake_plc.py` random-walks `Level.PV` over 0-4 m
with `Level.HH=9`, `Level.LL=0.5`, so using the real interlock band directly
would pin every `PID.SP` at 0.5 m and defeat the MPC trajectory.

`reference/water_mpc/mpc_core.py`'s `WaterTankController.__init__` needed a
small adaptation (not a rewrite) to run under the installed do-mpc version;
see `OPEN_QUESTIONS.md` for exactly what and why. The model, objective,
constraints, and solver settings from `build_model()`/`build_mpc()`/
`build_ekf()` are untouched.

## Mode handling and bumpless transfer

- `APC.Enabled` is read from the DCS's own OPC UA server every control
  cycle (`global_server.read_enabled()`).
- On the cycle where a unit's `APC.Enabled` transitions False -> True,
  `UnitController.request_bumpless_reset()` is called, which makes the next
  `step()` call `reset_to_measurement()` on the underlying controller with
  the unit's current `Level.PV` before solving. This seeds the MPC/EKF
  internal state at the real plant level so the first `PID.SP` it writes is
  close to the measurement, not a jump toward the far-away target (see
  `dcs/tests/test_bumpless_transfer.py`).
- While `APC.Enabled` is False, `dcs/` does not solve or write `PID.SP` for
  any unit, and `APC.Status` reads `DISABLED`.
- When a unit's MPC does not converge (do-mpc/ipopt raised, see
  `MPCResult.converged`) or its solve is still running when the cycle's
  solve budget (`DCS_SOLVE_BUDGET_S`, default 2.0 s) elapses, `dcs/` holds
  the last good `PID.SP` for that unit (does not write a new, unvalidated
  value) and `APC.Status` reports `SOLVER_FAIL` for that cycle.
- If one or more units are dropped by the watchdog (see below) while others
  are still running, `APC.Status` reports `DEGRADED` instead of `OK`, and
  the alive units keep solving and writing setpoints normally.

## Watchdog

`dcs/watchdog.py`'s `HeartbeatWatchdog` tracks each unit's
`Status.Heartbeat` (subscribed via OPC UA, cached in `UnitClient`). If a
unit's heartbeat value has not changed for `DCS_HEARTBEAT_STALL_CYCLES`
(default 5) consecutive control cycles, or the unit is disconnected, it is
excluded from that cycle's solve dispatch in `dcs/main.py`'s control loop.
The rest of the units keep solving and writing on their own schedule; a
dead unit never blocks or slows the others (each unit's MPC solve is
dispatched independently to a thread pool, see below). A unit is
automatically re-admitted once its heartbeat starts moving again.

## Cycle structure and concurrency

Every 1 s (`DCS_CYCLE_S`) `dcs/main.py`'s `control_loop`:
1. Reads `APC.Enabled` from the DCS's own server.
2. Checks each unit's watchdog state, drops dead/disconnected units.
3. For each alive, enabled unit, reads `Level.LL`/`Level.HH` (cheap OPC UA
   reads) and dispatches that unit's blocking `UnitController.step()` call
   (do-mpc/ipopt, not async-safe) to a `ThreadPoolExecutor` worker.
4. Waits for each unit's solve up to the shared per-cycle `solve_budget_s`,
   writes `PID.SP` for units that converged in time, logs a warning and
   holds the last `PID.SP` otherwise.
5. Logs every reachable unit's full tag set plus the global APC tags to the
   historian.
6. Publishes `APC.SolveTime_ms` (max across alive units) and `APC.Status`.
7. Sleeps for whatever remains of the 1 s cycle.

Note from live testing (see below): ipopt does not appear to release the
GIL for the whole solve in this environment, so 3 units' solves in the
thread pool do not run fully in parallel; wall time is closer to
N * ~0.35 s than to a single ~0.35 s. `DCS_SOLVE_BUDGET_S` defaults to 2.0 s
to give 3 units headroom under that; it is a per-cycle wall-clock guard
against a genuinely stuck/non-converging solve, not a tight performance
budget.

## Historian

`dcs/historian.py`'s `Historian` writes to one SQLite table:

```sql
CREATE TABLE history (ts REAL, unit TEXT, tag TEXT, value TEXT)
```

One row per tag per unit per cycle (`Unit1`..`UnitN`), plus one row per
global APC tag per cycle (`unit = 'Global'`). `value` is stored as text
(`str(value)`) since tags mix float/bool/uint/string types and this is a
flat demo-scale log, not a typed schema.

## Running standalone against `tools/fake_plc.py`

```bash
cd water_plant_DCS
python tools/fake_plc.py --units 3 --base-port 4840
# in another shell:
PLC_ENDPOINTS="opc.tcp://127.0.0.1:4840/,opc.tcp://127.0.0.1:4841/,opc.tcp://127.0.0.1:4842/" \
python dcs/main.py
```

`dcs/` starts its own OPC UA server on `opc.tcp://0.0.0.0:4900/` and
connects as a client to all three fake PLC units. `APC.Enabled` defaults to
`False`; write it to `True` via any OPC UA client (or `hmi/`) pointed at
`Global/APC/Enabled` on `opc.tcp://127.0.0.1:4900/` to start the control
loop.

### Known blocker in `tools/fake_plc.py` under the installed asyncua version

`tools/fake_plc.py` is frozen (not edited here). As shipped, it crashes on
its own within the first update cycle under the asyncua version
`requirements.txt` resolves to (`Status.Heartbeat.write_value(python int)`
infers `Int64`, but the node is declared `UInt32`, and the server rejects
its own write). Reproduced against both asyncua 2.0.1 and 1.1.5, so it is
not a version-pinning problem. See `OPEN_QUESTIONS.md` for the exact
one-line upstream fix. To get the live evidence below, a scratch copy of
`fake_plc.py` with only that one line patched
(`ua.Variant(heartbeat, ua.VariantType.UInt32)`) was run outside the repo;
the tracked `tools/fake_plc.py` was not modified.

### Evidence this was actually run

With the patched `fake_plc.py` serving 3 units and `dcs/main.py` connected
(`PLC_ENDPOINTS` pointed at all three, `APC.Enabled` written `True` from a
separate OPC UA client script), over roughly 40 s of real operation:

- Log showed all three units connecting
  (`Unit1/2/3: connected to opc.tcp://127.0.0.1:484x/`).
- `APC.Enabled` write from the external client landed and was read back by
  the control loop.
- The historian SQLite DB (`dcs_test.sqlite3`) accumulated 4950 rows across
  `Unit1`, `Unit2`, `Unit3`, `Global` in that window.
- `APC.Status` values actually observed in the DB: `DISABLED` (before
  enable), `OK` (34 cycles), `SOLVER_FAIL` (42 cycles, from the 2 s budget
  being tight for 3 concurrent solves under this environment's GIL
  behavior, see above — the hold-last-SP path engaged exactly as designed).
- `PID.SP` values read back from the fake PLC after being written by the
  DCS, ramping from the bumpless-transfer starting point toward the model's
  ceiling (`Level.PV` in `fake_plc.py` random-walks over 0-4 m, far outside
  the model's 0-0.2 m envelope, so the MPC saturates at
  `MODEL_LEVEL_MAX_M`, consistent with the scale-mismatch note above), e.g.:

  ```
  (ts, 'Unit1', 'PID.Mode', 'CASCADE')
  (ts, 'Unit1', 'PID.SP',   '0.1984665472750935')
  (ts, 'Unit1', 'Level.PV', '0.7948175580093084')
  ```

  confirming the DCS wrote `PID.SP` and the PLC's own `PID.Mode` stayed
  `CASCADE` (the PLC follows the DCS's setpoint) throughout.

### Tests

```bash
cd water_plant_DCS
python -m pytest dcs/tests -q
```

9 tests, all passing (`~90 s`, dominated by do-mpc/ipopt setup cost per
`UnitController`):
- `test_bumpless_transfer.py`: first `PID.SP` after enable (and after a
  disable/re-enable at a different level) stays within 0.02 m of the
  measurement.
- `test_dead_unit_isolation.py`: `HeartbeatWatchdog` drops a stalled/
  unreachable unit after the configured number of cycles, re-admits it once
  its heartbeat resumes, and one unit's dead state never affects another
  unit's alive state.
- `test_mpc_bounds.py`: a large setpoint change (near the model ceiling, and
  one deliberately above the model's physical range) never produces a
  `PID.SP` outside `[MODEL_LEVEL_MIN_M, MODEL_LEVEL_MAX_M]`; a forced
  non-convergence holds the last good `PID.SP` unchanged.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PLC_ENDPOINTS` | 3 local fake_plc endpoints | Comma-separated `opc.tcp://` URLs, one per unit, in unit order |
| `DCS_SERVER_ENDPOINT` | `opc.tcp://0.0.0.0:4900/` | The DCS's own OPC UA server endpoint |
| `DCS_CYCLE_S` | `1.0` | Control cycle period |
| `DCS_SOLVE_BUDGET_S` | `2.0` | Per-cycle wall-clock budget for all unit solves |
| `DCS_HEARTBEAT_STALL_CYCLES` | `5` | Consecutive stalled cycles before a unit is dropped |
| `DCS_HISTORIAN_DB` | `dcs_historian.sqlite3` | SQLite file path |
| `DCS_RECONNECT_BACKOFF_S` | `2.0` | OPC UA client reconnect backoff |
| `DCS_TARGET_LEVEL_M` | `0.15` | Fixed APC target level applied to every unit, see `OPEN_QUESTIONS.md` (no tag exists for a per-unit operator target) |

## Files

- `config.py` — env var loading.
- `opc_client.py` — `UnitClient`: OPC UA client to one PLC unit, subscribes
  `Level.PV`/`Status.Heartbeat`, reconnects on failure, writes `PID.SP`.
- `global_server.py` — `GlobalServer`: the DCS's own OPC UA server for the
  `APC.*` global tags.
- `controller_wrapper.py` — `UnitController`: wraps the ported MPC, derives
  `PID.SP` from the predicted trajectory, bumpless transfer, hold-last-SP.
- `watchdog.py` — `HeartbeatWatchdog`.
- `historian.py` — SQLite logger.
- `main.py` — wires everything into the 1 s control loop.
- `Dockerfile` — builds and runs `dcs/main.py`.
- `tests/` — see above.
