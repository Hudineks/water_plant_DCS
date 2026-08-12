"""Single-loop PID controller, positional form.

Anti-windup by conditional integration: the integral term only accumulates
when the output is not saturated, or when the error is already pulling the
output back inside its limits. This avoids the classic windup where a long
saturation period leaves a huge integral term that then overshoots badly once
the process value catches up.

Derivative acts on the process value, not the error, so a setpoint step does
not produce a derivative kick.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PID:
    kp: float
    ki: float
    kd: float
    out_min: float = 0.0
    out_max: float = 100.0

    _integral: float = field(default=0.0, init=False, repr=False)
    _prev_pv: float = field(default=0.0, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)

    def reset(self, pv: float = 0.0) -> None:
        """Clear integral and derivative history. Call on mode change or trip
        so the loop does not bump when it resumes control."""
        self._integral = 0.0
        self._prev_pv = pv
        self._initialized = False

    def compute(self, sp: float, pv: float, dt: float) -> float:
        if dt <= 0.0:
            return self._clamp(self.kp * (sp - pv))

        if not self._initialized:
            self._prev_pv = pv
            self._initialized = True

        error = sp - pv
        derivative = -(pv - self._prev_pv) / dt

        unclamped = self.kp * error + self.ki * self._integral + self.kd * derivative
        output = self._clamp(unclamped)

        saturated = output != unclamped
        pulls_back = (output >= self.out_max and error < 0.0) or (output <= self.out_min and error > 0.0)
        if not saturated or pulls_back:
            self._integral += error * dt

        self._prev_pv = pv
        return output

    def _clamp(self, value: float) -> float:
        return max(self.out_min, min(self.out_max, value))
