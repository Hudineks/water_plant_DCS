"""First-principles tank level model.

Mass balance on the tank volume: level change is driven by pump inflow minus
valve/gravity outflow, divided by the tank cross-section area. Fixed-step
Euler integration, called once per scan cycle by scan_engine.ScanEngine.

Not a CFD model. One well-mixed volume, no thermal effects, no pipe dynamics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class TankModel:
    area_m2: float = 2.0                 # tank cross-section area
    pump_max_flow_m3s: float = 0.05      # inflow at Pump.CMD = 100 %
    valve_cv: float = 0.03               # outflow coefficient at Valve.CMD = 100 %
    level_m: float = 1.5                 # current level, model state

    def step(self, pump_cmd_pct: float, valve_cmd_pct: float, dt_s: float) -> float:
        """Advance the tank level by one fixed step and return the new level.

        Outflow is gravity-driven: Q_out = Cv * valve_opening * sqrt(level),
        so outflow falls off as the tank empties, same as a real orifice/valve
        on a static head.
        """
        pump_cmd_pct = max(0.0, min(100.0, pump_cmd_pct))
        valve_cmd_pct = max(0.0, min(100.0, valve_cmd_pct))

        q_in = self.pump_max_flow_m3s * (pump_cmd_pct / 100.0)
        q_out = self.valve_cv * (valve_cmd_pct / 100.0) * math.sqrt(max(self.level_m, 0.0))

        d_level = (q_in - q_out) / self.area_m2 * dt_s
        self.level_m = max(0.0, self.level_m + d_level)
        return self.level_m
