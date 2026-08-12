# reference/water_mpc

Ported controller core from the original Vodarna project
(`src/models/mpc_water_tank_controller.py`), stripped of DAQ I/O, GUI plotting,
and CSV recipe playback. `mpc_core.py` keeps the do-mpc model, MPC objective,
constraints, solver settings, and EKF unchanged from the original.

`WaterTankController` is the class dcs/ should wire into the OPC UA loop:

```python
from reference.water_mpc.mpc_core import WaterTankController

controller = WaterTankController()
result = controller.step(setpoint_m=1.5, level_measured_m=1.42)
# result.flow_cm3s -> convert to whatever units PID.SP expects and write it
```

Notes for the port into dcs/:

- The physical model here works in centimeters internally (matches the
  original rig's geometry). `step()` takes and is driven by meters at its
  public boundary, matching tags.yaml. `result.flow_cm3s` is the original
  controller's actuator output (a simulated pump flow), which is not
  directly usable in this project: here the MPC must write Unit*.PID.SP,
  never an actuator command, per the cascade requirement in the main
  README. Use the flow-based optimization to derive the level trajectory
  the MPC wants (its internal `x_hat`/prediction over the horizon already
  encodes where it wants h2 to be), and write the near-term point of that
  trajectory as the next PID.SP. Document the exact mapping you choose in
  dcs/README.md, this reference implementation does not prescribe it.
- Call `reset_to_measurement()` once right before the first `step()` after
  `APC.Enabled` transitions to true, for bumpless transfer.
- One `WaterTankController` instance per unit. It is not thread-safe and not
  async; call it from a plain synchronous slot inside the DCS control cycle
  or off-load it to a worker thread/process if do-mpc's solve time threatens
  the 1 s cycle budget.
