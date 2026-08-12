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

## plc/: Valve.CMD is read-only, so it cannot be varied as a disturbance

`Valve.CMD` is `access: R` and described as a disturbance input, so plc/
treats it as a fixed constant (`VALVE_CMD_PCT` env var, default 30%)
instead of something that changes during a run. Demo A (outflow
disturbance, APC on vs off) needs a moving disturbance to be meaningful.
Either tags.yaml needs `Valve.CMD` to be `RW` (written by a
disturbance-injection tool, not by the PLC itself), or the PLC needs its
own internal disturbance schedule. Neither is in tags.yaml today; picking
one is an integration decision, not something a single-directory agent
should decide alone.

## dcs/: no tag for an operator/APC target level

tags.yaml has `PID.SP` (written by the DCS, the value the cascade tracks)
and `Level.SP` (read-only, "effective setpoint currently used by the PID"),
but no tag for "the level the operator/APC wants the unit to reach", i.e.
the MPC's external reference. `dcs/main.py` uses a fixed value
(`DCS_TARGET_LEVEL_M` env var, default 0.15 m) applied to every unit, since
there is nothing in the contract to read a per-unit target from. If a
target-level tag is added to tags.yaml later, `dcs/main.py`'s
`target_level_m` is the one line to change.

## dcs/: reference/water_mpc's physical envelope does not match this plant's scale

`reference/water_mpc/mpc_core.py` models a small lab rig (H1_MAX=15 cm,
H2_MAX=20 cm, i.e. 0-0.2 m). `tools/fake_plc.py` random-walks `Level.PV`
over 0-4 m and sets `Level.HH=9.0`, `Level.LL=0.5` for every unit. Clipping
`PID.SP` to the unit's real `Level.LL`/`Level.HH` (0.5-9 m) would put every
setpoint outside the ported controller's valid state range and defeat the
MPC trajectory entirely (SP would pin to 0.5 m forever). `dcs/controller_wrapper.py`
clips `PID.SP` to the model's own envelope (0-0.2 m) first and only
additionally tightens against `Level.LL`/`Level.HH` when those fall inside
that envelope, which they do not with fake_plc's current defaults. This
means the DCS demo runs the MPC over a 0-0.2 m band regardless of the
unit's real interlock thresholds. Rescaling the ported model to the
plant's actual tank geometry (or getting real tank geometry from plc/) is
out of scope here and would be the fix if setpoints need to span the full
`Level.PV` range.

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
