"""Per-unit heartbeat watchdog. A unit is dropped from the optimization loop
when Status.Heartbeat has not changed for heartbeat_stall_cycles consecutive
control cycles, and re-admitted automatically once it starts counting again.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UnitHeartbeatState:
    last_heartbeat: int | None = None
    stall_count: int = 0
    alive: bool = True


class HeartbeatWatchdog:
    def __init__(self, stall_cycles: int):
        self.stall_cycles = stall_cycles
        self._state: dict[int, UnitHeartbeatState] = {}

    def observe(self, unit_id: int, heartbeat: int | None) -> bool:
        """Feed the latest Status.Heartbeat reading for a unit (None if the
        read failed / the unit is unreachable this cycle). Returns True if
        the unit should be considered alive for this control cycle.
        """
        state = self._state.setdefault(unit_id, UnitHeartbeatState())

        if heartbeat is None:
            state.stall_count += 1
        elif state.last_heartbeat is None or heartbeat != state.last_heartbeat:
            state.last_heartbeat = heartbeat
            state.stall_count = 0
        else:
            state.stall_count += 1

        state.alive = state.stall_count < self.stall_cycles
        return state.alive

    def is_alive(self, unit_id: int) -> bool:
        state = self._state.get(unit_id)
        return state.alive if state else True
