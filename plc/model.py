"""Two-tank-in-series level model, matching the original Vodarna rig.

Physics: a pump fills tank 1. Tank 1 drains by gravity through a fixed
orifice into tank 2 (Torricelli's law, no valve). Tank 2 drains by its own
fixed orifice to the sump. One actuator only (the pump); both inter-tank
and outlet flows are passive gravity flow through fixed-size holes, same as
reference/water_mpc/mpc_core.py, which this file's constants are kept in
sync with (that module derives the same F_AREA/K_OUT from these same
D_TANK_MM/D_HOLE_MM/CD numbers; plc/ duplicates the four raw constants as
plain floats instead of importing casadi/do-mpc just to get two numbers).

Not a CFD model. Two well-mixed volumes, no thermal effects, no pipe
dynamics. Internal state is centimeters (matching the reference model and
the real rig's own units); the OPC UA boundary (Level.PV) is meters, same
as tags.yaml, converted only where that boundary is crossed (see
scan_engine.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Kept in sync with reference/water_mpc/mpc_core.py's D_TANK_MM/D_HOLE_MM/
# CD/G_CM. Same tank, same physics, same numbers.
D_TANK_MM = 50.0
D_HOLE_MM = 4.0
CD = 0.61
G_CM = 981.0  # cm/s^2

D_TANK_CM = D_TANK_MM / 10.0
D_HOLE_CM = D_HOLE_MM / 10.0

F_AREA = math.pi * (D_TANK_CM / 2.0) ** 2         # tank cross-section, cm^2
S_HOLE = math.pi * (D_HOLE_CM / 2.0) ** 2         # outlet hole area, cm^2
K_OUT = CD * S_HOLE * math.sqrt(2.0 * G_CM)       # outlet coefficient, cm^2.5/s
K_TRANSFER = K_OUT                                 # tank1 -> tank2 transfer coefficient

EPS = 1e-6


@dataclass
class TankModel:
    pump_max_flow_cm3s: float = 17.0  # inflow at Pump.CMD = 100%, matches mpc_core.py's U_MAX
    h1_cm: float = 1.0                # upstream tank level, model state, no sensor in the real rig
    h2_cm: float = 1.0                # downstream tank level, model state, this is Level.PV

    def step(self, pump_cmd_pct: float, dt_s: float) -> float:
        """Advance both tank levels by one fixed step, return h2_cm (the
        measured/controlled level). No valve input: outflow from tank 1
        into tank 2, and from tank 2 to the sump, is always gravity through
        a fixed orifice (Torricelli), same as the real rig.
        """
        pump_cmd_pct = max(0.0, min(100.0, pump_cmd_pct))
        q0 = self.pump_max_flow_cm3s * (pump_cmd_pct / 100.0)

        h1_safe = max(self.h1_cm, 0.0)
        h2_safe = max(self.h2_cm, 0.0)

        d_h1 = (q0 - K_OUT * math.sqrt(h1_safe + EPS)) / F_AREA * dt_s
        d_h2 = (K_TRANSFER * math.sqrt(h1_safe + EPS) - K_OUT * math.sqrt(h2_safe + EPS)) / F_AREA * dt_s

        self.h1_cm = max(0.0, self.h1_cm + d_h1)
        self.h2_cm = max(0.0, self.h2_cm + d_h2)
        return self.h2_cm
