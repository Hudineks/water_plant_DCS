# Open questions

Things that came up during the build that are worth a decision but did not
block progress. Each contributor (human or agent) appends here instead of
silently improvising past a contract gap.

## plc/: no writable mode-select tag

`PID.Mode` is `access: R` in tags.yaml, so no client can select MAN or
CASCADE. The tag's own description says "MAN = operator drives Pump.CMD
directly", but `Pump.CMD` is also `access: R`, so there is no tag an
operator's manual command could arrive on either. Resolution used: mode is
internal PLC state, set at startup from `UNIT_INITIAL_MODE` (default
CASCADE) and changed automatically only by the SP watchdog (CASCADE -> AUTO
on stale `PID.SP`). AUTO and MAN are both implemented in
`plc/scan_engine.py` and covered by tests, but nothing in the current
contract lets an external client select MAN or drive a manual pump command
in this v1. If mode should be operator-selectable, tags.yaml needs a
writable mode-select tag and a writable manual pump-command tag.

## plc/: no manual interlock reset tag

The task requires a latched trip with manual reset, but tags.yaml has no
tag for it (`Interlock.Trip` is `access: R`). Resolution used: the PLC
polls a reset file (`RESET_FILE` env var, default `./unit<N>.reset`) once
per scan; if the file exists, the trip clears and the file is deleted. This
stands in for a physical reset pushbutton wired to a DI that is not in the
tag list. If a real reset path is wanted, tags.yaml needs a writable
`Interlock.Reset` tag.

## plc/: no tag for the local (AUTO-mode) setpoint

Only `PID.SP` exists and it is documented as the cascade setpoint written
by the DCS. AUTO mode uses an internally held `local_sp`, seeded from
`UNIT_LOCAL_SP` and, when the watchdog demotes CASCADE to AUTO, set to the
last known good cascade value. There is no way for an external client to
change the AUTO setpoint independently of CASCADE writes in this v1.

## SUPERSEDED: plc/: Valve.CMD is read-only, so it cannot be varied as a disturbance

Superseded 2026-08-12: `Valve.CMD`/`Valve.FB` were removed from tags.yaml
entirely, not made writable. The real Vodarna rig this project models has
no valve at all -- a pump fills an upstream tank, both inter-tank and
outlet flows are fixed gravity orifices (Torricelli's law) -- so a
disturbance-input valve was never physically accurate to begin with. See
"plc/ and reference/: two-tank-in-series rework" below for the fix, and
`demos/plc_stub.py`'s `--disturbance-file` mechanism for how demo_a now
scripts a disturbance without a valve tag (a direct, internally-applied
step on `Level.PV`, since that tag is read-only and only the server
process that owns a node may write it locally).

## dcs/: no tag for an operator/APC target level

tags.yaml has `PID.SP` (written by the DCS, the value the cascade tracks)
and `Level.SP` (read-only, "effective setpoint currently used by the PID"),
but no tag for "the level the operator/APC wants the unit to reach", i.e.
the MPC's external reference. `dcs/main.py` uses a fixed value
(`DCS_TARGET_LEVEL_M` env var, default 0.15 m) applied to every unit, since
there is nothing in the contract to read a per-unit target from. If a
target-level tag is added to tags.yaml later, `dcs/main.py`'s
`target_level_m` is the one line to change.

## RESOLVED: dcs/: reference/water_mpc's physical envelope does not match this plant's scale

Resolved 2026-08-12: `plc/model.py` was rewritten to use the exact same
tank geometry constants as `reference/water_mpc/mpc_core.py`
(`D_TANK_MM=50`, `D_HOLE_MM=4`, `Cd=0.61`), so `Level.PV`'s real operating
band is now ~0-0.20 m, matching the ported controller's own valid envelope
instead of `tools/fake_plc.py`'s old placeholder 0-4 m random walk.
`plc/unit.py`'s `LEVEL_HH`/`LEVEL_LL` defaults moved to 0.18 m / 0.01 m to
match. `dcs/controller_wrapper.py`'s clip-to-model-envelope logic is
unchanged and still correct, it just now agrees with the plant's real
interlock thresholds instead of silently overriding them. See "plc/ and
reference/: two-tank-in-series rework" below for the full change.

## dcs/: reference WaterTankController.__init__ incompatible with do-mpc 5.1.1

requirements.txt pins `do-mpc>=4.6.0` and the installed version resolved to
5.1.1. Against that version, `reference/water_mpc/mpc_core.py`'s
`WaterTankController.__init__` (frozen, not edited) fails two ways: (1)
`mpc.set_tvp_fun()` calls the tvp function eagerly to validate its return
type, but `self._sp_cm` (read by that function) is only assigned after the
`set_tvp_fun()` call in the original ordering, raising `AttributeError`;
(2) `mpc.data.prediction()`, which dcs/ needs to read the predicted h2
trajectory for `PID.SP` (see `dcs/controller_wrapper.py` docstring),
requires `store_full_solution=True`, which `build_mpc()` does not set.
`dcs/controller_wrapper.py` works around both by subclassing
`WaterTankController` and overriding only `__init__` (reusing
`build_model()`/`build_mpc()`/`build_ekf()` unchanged), fixing the
assignment order and adding the one setting. No model, objective,
constraint, or solver-option code was touched.

## tools/fake_plc.py: Status.Heartbeat write crashed on startup (fixed)

`run_unit()`'s loop did `await nodes["Status.Heartbeat"].write_value(heartbeat)`
with `heartbeat` a plain Python int. asyncua's `write_value()` with a bare
int infers `VariantType.Int64`, but `Status.Heartbeat` is declared
`type: uint` in tags.yaml, so `build_unit_nodes()` creates that node as
UInt32. The server then rejected its own write (`BadTypeMismatch`),
unhandled, crashing `asyncio.gather()` and taking down all N units within
the first update cycle. Reproduced under both asyncua 2.0.1 (what
`pip install -r requirements.txt` resolves today) and the 1.1.5 floor
pinned in requirements.txt, so this was a real bug, not a version-pinning
issue. Fixed during integration: the write now uses
`ua.Variant(heartbeat, ua.VariantType.UInt32)`.

## docker-compose.yml: --scale plc=5 is not feasible with a fixed 3-service compose file

The current compose structure gives every `plc` replica the same
`UNIT_ID`/`OPCUA_PORT` env vars and `dcs` a static `PLC_ENDPOINTS` list, so
`docker compose up --scale plc=5` starts 5 identical units fighting over
the same port rather than 5 distinct units. Scale-friendly handling would
need a per-replica `UNIT_ID` derived from the container hostname (Compose
sets `HOSTNAME` to `<service>-<n>`) plus either a fixed internal port with
Docker's own port allocation, or `dcs` doing OPC UA endpoint discovery
instead of reading a static list. Not implemented in v1; `demos/demo_d_scale_to_5.py`
documents the gap and demonstrates scale-up against `tools/fake_plc.py`
instead, which does not have this limitation since every unit there is
already spawned from one `--units N` flag.

## Integration fixes (2026-08-12), found and fixed while wiring plc/, dcs/, hmi/ together

Each agent built and tested its own directory against `tools/fake_plc.py` in
isolation, which is exactly what SHARED CONSTRAINTS asked for, but it means
nobody exercised the real cross-service wiring end to end. Once all three
were combined and run together against real `plc.unit` processes and a real
`dcs.main` process, three integration-only bugs surfaced (none visible from
any single directory's own tests):

1. `docker-compose.yml` set `DCS_OPCUA_PORT` on the `dcs` service, but
   `dcs/config.py` reads `DCS_SERVER_ENDPOINT`. The env var name agreed on
   in `dcs/README.md` and the one used in the compose file never matched, so
   `dcs` would have silently fallen back to its own default endpoint in a
   real `docker compose up`. Fixed by renaming the compose var to
   `DCS_SERVER_ENDPOINT: "opc.tcp://0.0.0.0:4850/"`, matching what
   `hmi`'s `DCS_ENDPOINT` already expected on the other side.
2. `hmi/opcua_bridge.py`'s `_poll_dcs()` called
   `resolve_unit_nodes(client.get_objects_node(), GLOBAL_TAGS)` directly on
   the server's root Objects node, but `dcs/global_server.py` publishes the
   global tags under an object node named "Global" (`Objects/Global/APC/...`),
   the same nesting convention `plc/unit.py` uses for "UnitN". Every read
   failed with `BadNoMatch`. Fixed by adding the same
   find-the-named-child-object step already used for units
   (`_find_child_object`, generalized from the existing `_find_unit_object`)
   and using it before resolving global tag nodes.
3. `tools/fake_plc.py`'s `Status.Heartbeat` write (see the entry above) was
   fixed in place once all three consumers had already worked around or
   hit it, since it blocks live end-to-end testing entirely otherwise.

None of this was a wrong call by any one agent: tags.yaml documents the
global tags' location only as "live on the DCS's own server-side status",
not the exact node path, and no directory owns docker-compose.yml's env var
naming versus dcs/'s own config module. This is the kind of gap that only
shows up once independently-built, independently-tested pieces are run
together, which is what the sequential integration phase is for.

Live end-to-end evidence after these fixes (3 real `plc.unit` processes,
one real `dcs.main`, one real `hmi` instance, no `fake_plc.py` involved):
`APC.Enabled` written to true via the DCS's own OPC UA server, `APC.Status`
went to `OK` with `APC.SolveTime_ms` around 400 ms per cycle, `PID.SP` on a
live PLC unit moved from its startup default (2.0) to the MPC's computed
setpoint (clipped to the reference model's 0-0.2 m envelope, see the entry
above on the scale mismatch) and stayed there under `PID.Mode=CASCADE`, and
`GET /api/state` on the HMI showed all three units and the DCS connected
with live values.

## plc/ and reference/: two-tank-in-series rework (2026-08-12)

The single-tank pump+valve model from the initial build did not match the
physical system this project is a DCS/APC counterpart to (the original
Vodarna rig: a pump fills tank 1, which has no sensor, only an EKF
estimate; tank 1 drains by gravity through a fixed orifice into tank 2,
which has the real sensor and is the PID/MPC-controlled level; tank 2
drains by its own fixed orifice to the sump; no valve anywhere).
`plc/model.py` was rewritten as a faithful two-tank-in-series ODE using
the exact same physical constants as `reference/water_mpc/mpc_core.py`.
`Valve.CMD`/`Valve.FB` were removed from tags.yaml (see the superseded
entry above). `plc/interlocks.py`'s `DRY_RUN_LEVEL_M` and `plc/unit.py`'s
`LEVEL_HH`/`LEVEL_LL`/`INITIAL_LEVEL_M` defaults, all tuned for the old
meter-scale single tank, moved to the new ~0-0.20 m band. The local PID's
gains (`PID_KP`/`PID_KI`/`PID_KD`) also needed retuning for the much
smaller, much faster-responding tank (empirically swept, landed on
kp=600/ki=10/kd=100, see `plc/tests/test_scan_engine.py`); the old
kp=40/ki=5 gains produced negligible authority against cm-scale errors and
either drained the tank into a permanent LL trip or oscillated into an HH/LL
limit cycle depending on how far they were pushed.

## reference/water_mpc/mpc_core.py: MPC previously got no benefit from lookahead (fixed)

`WaterTankController._tvp_fun_mpc` broadcast a single flat setpoint value
across the MPC's entire prediction horizon, meaning the "predictive" part
of the controller was never actually exercised: it reacted to the current
error every cycle exactly like a plain PID would, just through a much
heavier solver. The original rig's real advantage (`load_cycle_to_mpc`/
`tvp_fun` in `src/models/mpc_water_tank_controller.py`) was giving the
solver a true preview of a future setpoint change before it happens.
`WaterTankController.set_cycle()` was added (additive, existing scalar-SP
callers see no behavior change) so `dcs/` can hand a unit a
`cycles.loader.SetpointCycle` and get real horizon preview back, ported
from the original CSV format and lookup logic. See `dcs/config.py`'s
`unit_setpoint_sources` for how each unit gets its setpoint source, and
`cycles/` for the loader and the two example CSVs (reused unmodified from
`src/templates/`).

## dcs/: Diagnostics.H1_Estimated is not in tags.yaml on purpose

The real rig's upstream tank (h1) has no sensor, only an EKF estimate
computed alongside the MPC. tags.yaml's unit contract describes what a PLC
actually publishes, and the real PLC never publishes h1 either, so adding
an `h1` tag there would misrepresent the physical system. Instead
`dcs/global_server.py` publishes the estimate itself, per unit, as
`Unit{n}/Diagnostics/H1_Estimated` on the DCS's own OPC UA server
(alongside the existing `APC.*` globals), built with a small ad hoc helper
rather than routed through `plantbus`/tags.yaml. The HMI reads it purely
for display.

## dcs/: closed-loop instability from PREDICTION_HORIZON_INDEX=1 (fixed)

Found during live end-to-end testing of the two-tank rework: running the
full real stack (real `plc.unit` x3 + real `dcs.main`, APC enabled) for
more than about a minute, every unit's `Level.PV` and `PID.SP` drifted
together down to the LL interlock and tripped, regardless of the unit's
actual target (constant or cycle). Root cause, confirmed by an isolated
in-process reproduction (real `UnitController` driving a real
`ScanEngine`, no OPC UA, fast to iterate) and by inspecting the MPC's raw
horizon prediction directly:

`dcs/controller_wrapper.py`'s `_extract_predicted_sp_m` read horizon index
1 (`PREDICTION_HORIZON_INDEX = 1`, "one t_step ahead") as the next
`PID.SP`. The MPC's own internal plan was correct (pump near `U_MAX` for
many steps, `h2` climbing steadily across the full 40-step horizon), but
index 1 of a slow, gradual 40-second climb is barely different from the
current measurement (a fraction of a millimeter). The PLC's local PID
then saw a near-zero error and applied near-zero pump command, directly
contradicting the MPC's own internal plan of sustained near-max flow. The
real plant, receiving almost no inflow, drained under gravity outflow.
The EKF's next state estimate was then seeded from that lower real
measurement, and the cycle repeated: predicted-SP-tracks-real-PV,
real-PV-drains-because-no-real-command, one step down every cycle,
compounding until LL.

This was a latent bug from the original v1 build, not something the
two-tank rework introduced, but it was invisible until now: the
single-tank model's much slower, much larger-scale dynamics meant a
one-second predicted increment was still a meaningful, trackable move
relative to typical PID gains and interlock spacing, and the short (tens
of seconds) smoke tests run during v1 verification never ran long enough
to reveal the slow compounding drift.

Fixed by raising `PREDICTION_HORIZON_INDEX` to 25 (empirically swept
5/10/15/20/25/30 against the real closed loop in the isolated
reproduction above; 1-10 still trip or dip badly, 15+ recovers, 25/30 are
the most robust, 25 chosen to stay clear of the horizon's less-certain
terminal edge). This gives the local PID a real, trackable setpoint gap
consistent with the MPC's own dynamically-planned trajectory, restoring
the intended cascade behavior. `dcs/tests/test_bumpless_transfer.py`'s
`MAX_FIRST_STEP_JUMP_M` was loosened from 0.02 to 0.05 m to match: a
meaningfully-sized first step toward a distant target is now correct
behavior, not something bumpless transfer should suppress (see that
test's updated docstring for why this is not the same thing as an
un-bumpless snap).

Live evidence after the fix: 3 real `plc.unit` processes + real
`dcs.main` + real `hmi`, APC enabled, run for 90+ seconds. `APC.Status`
stayed `OK` throughout (no `SOLVER_FAIL`), all three units' `Level.PV`
climbed steadily and monotonically (0.042 m -> 0.066-0.074 m over 90s),
no interlock trips, and Unit 3 (constant target) visibly pulled ahead of
Units 1/2 (still in the flat opening phase of their step/ramp cycles) as
expected.

## dcs/: solve_budget_s default mismatch caused persistent SOLVER_FAIL (fixed)

Found in the same investigation above: `dcs/config.py`'s `Config`
dataclass default for `solve_budget_s` is `2.0`, matching `dcs/README.md`'s
documented behavior ("2.0 s to give 3 units headroom"), but
`load_config()`'s env-var fallback read `"0.8"` instead, so every real run
without `DCS_SOLVE_BUDGET_S` set (the common case, including
`docker-compose.yml`) actually used an 0.8 s budget. Three concurrent
do-mpc solves take roughly `N * ~0.35 s` wall time under this environment
(ipopt does not release the GIL for the whole solve, see `dcs/README.md`),
so 3 units cost ~1.05 s, comfortably exceeding an 0.8 s budget and
triggering `SOLVER_FAIL`/hold-last-SP almost every cycle. Fixed by
changing the fallback to `"2.0"` to match the documented default.

## hmi/: per-unit live cycle control replaces the single global APC.Enabled switch (2026-08-12)

Running the stack standalone (no Docker), the operator reported two real
problems: no way to enable/disable or pick a setpoint source per unit
(only one global `APC.Enabled` toggle existed), and the manual-SP input
field lost whatever was typed into it before the write could be
submitted.

The second one was a real, separate bug: `hmi/static/app.js`'s `render()`
fully replaces `#units`' `innerHTML` on every websocket push (~1/s), which
destroys and recreates every `<input>`, so anything the operator had
started typing was gone before they could click WRITE. Fixed with
`captureFocusedInput()`/`restoreFocusedInput()`: before a rebuild, save
the currently-focused element's identifying `data-*` attribute, value, and
cursor position; after rebuilding, reapply them to the matching new
element. General enough to cover the manual-SP box, the new manual-target
field, and the new cycle `<select>`.

The first is a real missing feature, not a bug, resolved by adding
`Unit{n}/Control/CycleName` (`"off"|"step"|"ramp"|"manual"`) and
`Control/ManualTargetM` to `dcs/global_server.py` (same not-in-tags.yaml
precedent as `Diagnostics.H1_Estimated`). The operator picked the model:
one dropdown per unit instead of a separate "APC enabled" concept, with
the existing global `APC.Enabled`/`APC.Status` becoming a *derived*
readout (`dcs/main.py`: true whenever at least one unit isn't `"off"`)
instead of an independent input. Writing `APC.Enabled` directly (the old
path) still exists for compatibility but has no lasting effect since the
DCS overwrites it with the derived value every cycle. See
`dcs/README.md`'s "Per-unit setpoint source" and "Mode handling" sections
for the full mechanism, and `hmi/README.md` for the new
`/api/units/{id}/cycle` endpoint.

The manual-SP box is now gated (disabled unless a unit's `CycleName` is
`"off"`) instead of silently getting overwritten by the DCS within ~1 s,
which is what the operator was actually hitting before ("i když to
stihnu tak se to nezapíše" -- the write did land, CASCADE mode was
correctly re-writing `PID.SP` from the active cycle every cycle, which
looked indistinguishable from "the write didn't work"). Verified live:
setting a unit's cycle to `"off"` then writing a manual SP now holds
(`PID.Mode` stays `CASCADE`, `PID.SP` stops moving) instead of being
overwritten on the next DCS cycle.

The tank1/upstream visualization request was addressed by extending the
mimic diagram to a two-tank stack (dashed, dim-filled upstream tank using
the already-wired `H1 (est.)` value, connected by a pipe glyph to the
existing downstream tank) rather than adding a new field -- the value was
already on the panel as text, just not drawn.
