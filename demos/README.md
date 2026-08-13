# Demos

Each script here starts its own fake process(es) and the HMI server, runs a
scenario, and prints what happened. None of them depend on docker-compose or
on plc/ or dcs/ existing (those are being built in parallel worktrees, see
the task's shared constraints); they use tools/fake_plc.py's pattern, either
directly or through demos/plc_stub.py (a one-unit-per-process variant, see
below), to develop and test the HMI side of each scenario. Where a scenario
genuinely needs the real plc/ or dcs/ to show its full effect (a and c
below), the script says so in its docstring and prints what to look for once
those exist.

Run each with `python demos/<script>.py` from water_plant_DCS/. They pick
their own ports (48xx/80xx ranges not used by the default fake_plc.py setup)
so they do not collide with a manually started fake_plc.py + hmi.

## Scripts

- **plc_stub.py** -- not a demo itself, a helper. tools/fake_plc.py bundles
  all N units into one OS process (`--units N`), which is fine for normal
  hmi/ development but makes "kill one unit" impossible without killing all
  of them. plc_stub.py serves exactly one unit, chosen by `--unit-id`, so
  demos b and d can start/stop units independently, matching how plc/ (one
  process per unit) and docker-compose will behave.

- **demo_a_outflow_disturbance.py** -- outflow disturbance, APC on vs APC
  off. Steps Level.PV directly partway through a run (the real rig has no
  valve to script a disturbance through, see tags.yaml) and prints the
  Level.PV trend. Only the open-loop half is real here (no dcs/ yet); rerun
  once dcs/ exists with APC.Enabled toggled to see the actual on/off
  comparison.

- **demo_b_lost_unit.py** -- kills unit 2's process ten seconds in (the
  fake-PLC equivalent of `docker stop plc-2`) and polls the HMI's
  /api/state every second for 20s. Expected: unit 2 flips to OFFLINE, units
  1 and 3 stay ONLINE throughout, and the HMI process itself never stops
  responding.

- **demo_c_setpoint_bound.py** -- writes a setpoint above Level.HH through
  the HMI's manual-SP endpoint. Confirms the write path itself (PID.SP is
  RW per tags.yaml, the HMI does not second-guess it). The interlock/clamp
  behavior that should stop this from being followed literally lives in
  plc/, not here; rerun against the real plc/ to see Interlock.Trip fire.

- **demo_d_scale_to_5.py** -- starts 5 plc_stub processes instead of 3 and
  points the HMI at all 5 via PLC_ENDPOINTS. Expected: 5 unit cards, all
  online, no HMI code change. See OPEN_QUESTIONS.md for why this is not the
  same claim as "`docker compose up --scale plc=5` works" -- the compose
  file's fixed three service names do not support that flag directly.

- **demo_e_setpoint_cycles.py** -- the demo this project is actually built
  to show. Starts three real plc/unit.py processes and a real dcs/main.py,
  using dcs/config.py's default assignment: Unit 1 tracks
  cycles/step_response.csv, Unit 2 tracks cycles/ramp_response.csv, Unit 3
  holds a constant setpoint. Enables APC and prints Level.PV/Level.SP for
  all three every few seconds. This is the one to screen-record: it is the
  only demo where the MPC gets a real preview of a future setpoint change
  across its solve horizon (see reference/water_mpc/mpc_core.py's
  set_cycle), instead of reacting to today's error after the fact. A
  screen recording of the HMI running this scenario is in
  [`demo_e_setpoint_cycles.gif`](demo_e_setpoint_cycles.gif) (also embedded
  in the top-level [`README.md`](../README.md)): cropped to just the panel
  (browser chrome and taskbar removed), played back at 2x speed, 10fps,
  native 2996x1272 resolution, via ffmpeg's two-pass palette workflow
  (`palettegen stats_mode=diff` then `paletteuse dither=bayer`, not a
  direct one-pass conversion) so the readout text stays sharp instead of
  smearing into dither noise -- 10MB, well under GitHub's 100MB per-file
  limit. The original full-quality `.mp4` (163MB) is gitignored rather
  than committed.

## What was actually run during development

`demo_b_lost_unit.py` and `demo_d_scale_to_5.py` were run in this worktree.
The individual pieces they depend on (plc_stub.py serving a unit, the HMI
connecting to it, /api/state reflecting connect/disconnect) were verified
directly against a live HMI + fake_plc.py session (see hmi/README.md's
testing section for the exact commands and output). Running all 5
plc_stub.py processes plus the HMI truly concurrently inside this sandboxed
shell was flaky (background process management in this dev container, not
an HMI bug -- process starts were occasionally silently dropped when several
were queued in the same shell invocation); the underlying mechanism each
demo exercises (PLC_ENDPOINTS drives an arbitrary-length, unit-count-free
list of pollers in hmi/opcua_bridge.py, with no code path anywhere assuming
exactly 3 units) was confirmed by reading hmi/opcua_bridge.py, hmi/main.py,
and hmi/static/app.js end to end, and by the live 3-unit test. Re-run these
two scripts directly in a normal terminal (not a sandboxed one) for a full
concurrent smoke test.
