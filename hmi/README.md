# hmi/ -- operator panel

FastAPI server + plain JS/HTML/CSS frontend (no build step, no npm) that
displays live plant state and lets an operator toggle APC and enter manual
setpoints. Port 8080.

## Data flow

```
 PLC unit 1 OPC UA server \
 PLC unit 2 OPC UA server  }--- polled 1x/s each ---\
 PLC unit 3 OPC UA server /                          \
                                                        v
 DCS OPC UA server (global APC.* tags        --- polled 1x/s --> hmi/opcua_bridge.py
  + per-unit Diagnostics.H1_Estimated)
                                                        |    (asyncio tasks,
                                                        |     one per source,
                                                        |     PlantState)
                                                        v
                                                  hmi/main.py
                                              (FastAPI, in-process
                                               state, no database)
                                                        |
                                          websocket push on any change
                                                        v
                                              browser (hmi/static/app.js)
```

- `hmi/opcua_bridge.py` runs one `asyncio` task per PLC unit endpoint plus
  one for the DCS endpoint. Each task opens an `asyncua.Client`, resolves
  the unit's/DCS's tag nodes with `plantbus.client.resolve_unit_nodes`, and
  loops: `read_all()` every 1 s, write the values into a shared
  `PlantState`, call `state.on_change()`. If a source is unreachable the
  task marks it disconnected, keeps retrying every 3 s, and does not affect
  the other sources' tasks -- one dead unit does not take down the panel.
- `hmi/main.py` owns a single `PlantState` instance and a set of connected
  `WebSocket`s. `on_change()` sets an `asyncio.Event`; one broadcaster task
  wakes on that event, builds a JSON snapshot of every unit + the DCS, and
  sends it to every open websocket. There is no polling from the browser
  side and no per-client OPC UA connection; the browser only ever speaks
  JSON over `ws://.../ws`.
- Operator actions (manual setpoint write, APC on/off) go the other way:
  the browser POSTs to `/api/units/{id}/setpoint` or `/api/apc/enabled`,
  `hmi/main.py` calls `PlantState.write_unit_setpoint`/`write_apc_enabled`,
  which write directly to the OPC UA node the bridge already resolved (no
  extra browse per write). A write against a disconnected source returns
  HTTP 409 with an error message instead of silently doing nothing.
- The frontend (`hmi/static/`) is plain HTML/CSS/JS: one `index.html`, one
  `style.css` (dense, monospace, no gradients/animation, per the operator-
  panel brief), one `app.js` that opens the websocket, renders unit mimic
  cards (tank fill bar + numeric readouts + a 10-minute PV/SP trend drawn
  as an inline SVG polyline, no charting library), the alarms table, and
  the APC toggle/status.

## Environment variables

- `PLC_ENDPOINTS` -- comma-separated `opc.tcp://` URLs, one per unit, in
  order (first entry is Unit1, and so on -- position drives the unit_id
  used to look up the `UnitN` object node on that server, matching
  tools/fake_plc.py's / plc/'s naming). Default assumes
  `python tools/fake_plc.py --units 3 --base-port 4840` on localhost:
  `opc.tcp://localhost:4840/,opc.tcp://localhost:4841/,opc.tcp://localhost:4842/`.
- `DCS_ENDPOINT` -- the DCS's own OPC UA server, for the global APC.* tags
  and each unit's `Diagnostics.H1_Estimated` (the EKF's estimate of the
  upstream tank's level, which has no sensor in the real rig and so is not
  in tags.yaml, see `dcs/README.md`). Shown per unit as "H1 (est.,
  unmeasured)" on the operator panel.
  Default `opc.tcp://localhost:4850/`. tags.yaml does not fix this
  port/shape; see OPEN_QUESTIONS.md for the assumption this makes and why.

## Run standalone against tools/fake_plc.py

From `water_plant_DCS/`:

```
python tools/fake_plc.py --units 3 --base-port 4840
# in another terminal:
python -m uvicorn hmi.main:app --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080/`. Without a DCS running, the APC block
shows "DCS: NO LINK" and the APC toggle is disabled (by design -- there is
nothing to write APC.Enabled to); the three unit cards show live values.

## What was actually tested (this worktree)

`docker` is not installed in this dev environment and plc/ and dcs/ are not
built in this worktree (built in parallel worktrees per the task's shared
constraints), so hmi/ was tested directly against tools/fake_plc.py:

1. Started `python tools/fake_plc.py --units 3 --base-port 4840`.
   It crashed on the first heartbeat write (`BadTypeMismatch`, see
   OPEN_QUESTIONS.md for the root cause and fix); patched the local copy of
   fake_plc.py in this worktree only, then restarted it successfully:
   `[fake_plc] Unit1/2/3 serving on opc.tcp://0.0.0.0:4840-4842/`.
2. Started `python -m uvicorn hmi.main:app --host 0.0.0.0 --port 8080`.
3. `curl http://localhost:8080/api/state` returned all three units
   `"connected":true,"alive":true` with live `Level.PV`/`Pump.CMD`/etc
   values and a 10-entry rolling history per unit, and `dcs.connected:false`
   (no DCS running, as expected).
4. A short Python client (`websockets.connect('ws://localhost:8080/ws')`)
   received a JSON snapshot on connect: `WS OK, units: ['1', '2', '3']
   unit1 PV: 0.1095152038496422` -- confirms the websocket push path
   carries live values end to end, not just the REST snapshot.
5. `curl -X POST http://localhost:8080/api/units/1/setpoint -d
   '{"value": 3.5}'` returned `{"ok":true}`; the next `/api/state` read
   back `PID.SP: 3.5` on unit 1, confirming the manual-SP write path
   reaches the OPC UA node.
6. `curl -X POST http://localhost:8080/api/apc/enabled -d '{"enabled":
   true}'` (no DCS running) returned HTTP 409
   `{"ok":false,"error":"DCS not connected, cannot write APC.Enabled"}`,
   confirming the write path fails loudly instead of pretending to succeed.
7. `curl http://localhost:8080/` and `/app.js` returned HTTP 200, confirming
   the static frontend is served alongside the API/websocket from the same
   FastAPI app.

demos/demo_b_lost_unit.py and demos/demo_d_scale_to_5.py exercise the
multi-process (kill one unit / add units beyond 3) side of this; see
demos/README.md for what was and wasn't run concurrently in this sandbox,
and why (background process flakiness in this dev container, not an issue
in the HMI code -- confirmed by direct code reading of
hmi/opcua_bridge.py's per-endpoint task loop, which has no hardcoded unit
count anywhere).

## Dependencies

FastAPI, uvicorn, websockets, asyncua, PyYAML, pydantic -- all already in
the repo-root requirements.txt except pydantic, which FastAPI depends on
directly and pip installs transitively; hmi/Dockerfile pins it explicitly
since it is imported directly in hmi/main.py (`SetpointRequest`,
`ApcEnabledRequest`). No frontend dependencies: static HTML/CSS/JS served
by FastAPI's StaticFiles, no bundler, no npm.
