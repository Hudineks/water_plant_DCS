"""Setpoint cycle CSV loader.

Ported from src/models/mpc_water_tank_controller.py's load_cycle_config /
get_cycle_setpoint (the original project's recipe-playback mechanism), same
file format, same semantics, translated into a small reusable class instead
of module-level globals.

CSV format (semicolon-delimited):
    perioda;<period in minutes>
    t;h
    <t0>;<h0>
    <t1>;<h1>
    ...

t is in minutes, h is in centimeters (matching reference/water_mpc/mpc_core.py's
cm-scale state), same units the original rig's CSV files already use.
value_at() wraps time on the period and linearly interpolates between the
two bracketing rows, so a caller can sample arbitrarily far into the future
(this is what gives an MPC true horizon preview instead of a frozen current
value, see reference/water_mpc/mpc_core.py's set_cycle()).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SetpointCycle:
    name: str
    period_s: float
    rows: list[tuple[float, float]]  # (t_seconds, h_cm), sorted by t

    @classmethod
    def from_csv(cls, path: str | Path) -> "SetpointCycle":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")

            period_row = next(reader)
            if not period_row[0].strip().lower().startswith("perioda"):
                raise ValueError(f"{path}: first row must be 'perioda;<minutes>'")
            period_minutes = float(period_row[1])

            header = next(reader)
            if header[0].strip() != "t" or header[1].strip() != "h":
                raise ValueError(f"{path}: second row must be header 't;h'")

            rows_minutes: list[tuple[float, float]] = []
            for row in reader:
                if len(row) < 2:
                    continue
                rows_minutes.append((float(row[0]), float(row[1])))

        if len(rows_minutes) < 2:
            raise ValueError(f"{path}: cycle needs at least 2 data points")

        rows_seconds = [(t * 60.0, h) for t, h in rows_minutes]
        return cls(name=path.stem, period_s=period_minutes * 60.0, rows=rows_seconds)

    def value_at(self, t_now_s: float, t_start_s: float = 0.0) -> float:
        """Return the setpoint (cm) at t_now_s, wrapping on the cycle's
        period relative to t_start_s and linearly interpolating between
        the two bracketing rows. Clamps to the last row's value if the
        cycle time falls beyond the last defined point."""
        cycle_time = (t_now_s - t_start_s) % self.period_s

        rows = self.rows
        for i in range(len(rows) - 1):
            t1, h1 = rows[i]
            t2, h2 = rows[i + 1]
            if t1 <= cycle_time <= t2:
                if t2 == t1:
                    return h1
                return h1 + (h2 - h1) * (cycle_time - t1) / (t2 - t1)

        return rows[-1][1]
