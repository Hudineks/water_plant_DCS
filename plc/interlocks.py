"""Safety interlock logic: HH / LL level trips and pump dry-run detection.

Latched: once tripped, Interlock.Trip stays true and the pump stays stopped
until reset() is called explicitly. This is a deliberate safety property, not
a bug, an interlock that self-clears the moment the process value recovers
is not a real interlock.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DRY_RUN_PUMP_THRESHOLD_PCT = 5.0   # pump considered "commanded on" above this
DRY_RUN_LEVEL_M = 0.10             # level at/below which a running pump is dry
DRY_RUN_SCAN_COUNT = 20            # consecutive scans before a dry-run trip (~2 s at 100 ms)


@dataclass
class InterlockLogic:
    hh: float
    ll: float
    tripped: bool = False
    reason: str = ""
    _dry_run_scans: int = field(default=0, init=False, repr=False)

    def evaluate(self, level_m: float, pump_cmd_pct: float) -> tuple[bool, str]:
        """Update the latch from current process values.

        pump_cmd_pct is the command from the previous scan (the state the
        pump was actually in going into this scan), matching how a real PLC
        interlock rung reads pump feedback before deciding whether to trip.
        """
        if self.tripped:
            return self.tripped, self.reason

        if level_m >= self.hh:
            self._trip("HH level")
        elif level_m <= self.ll:
            self._trip("LL level")
        else:
            dry_candidate = pump_cmd_pct > DRY_RUN_PUMP_THRESHOLD_PCT and level_m <= DRY_RUN_LEVEL_M
            if dry_candidate:
                self._dry_run_scans += 1
                if self._dry_run_scans >= DRY_RUN_SCAN_COUNT:
                    self._trip("pump dry-run")
            else:
                self._dry_run_scans = 0

        return self.tripped, self.reason

    def _trip(self, reason: str) -> None:
        self.tripped = True
        self.reason = reason

    def reset(self) -> None:
        self.tripped = False
        self.reason = ""
        self._dry_run_scans = 0
