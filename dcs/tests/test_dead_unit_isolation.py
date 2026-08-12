"""A dead unit (stalled Status.Heartbeat) must drop out of the optimization
loop without affecting the other units. This tests watchdog.py in
isolation, which is what dcs/main.py consults each cycle before dispatching
solves.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dcs.watchdog import HeartbeatWatchdog


def test_stalled_heartbeat_drops_unit_after_threshold():
    wd = HeartbeatWatchdog(stall_cycles=3)

    # Unit1 increments normally, Unit2 stalls at heartbeat=10.
    assert wd.observe(1, 10) is True
    assert wd.observe(2, 10) is True

    assert wd.observe(1, 11) is True
    assert wd.observe(2, 10) is True  # 1st stall

    assert wd.observe(1, 12) is True
    assert wd.observe(2, 10) is True  # 2nd stall

    assert wd.observe(1, 13) is True
    assert wd.observe(2, 10) is False  # 3rd stall, dropped

    # Unit1 keeps running unaffected.
    assert wd.observe(1, 14) is True


def test_unreachable_unit_counts_as_stalled():
    wd = HeartbeatWatchdog(stall_cycles=2)
    assert wd.observe(1, 5) is True
    assert wd.observe(1, None) is True   # unreachable this cycle, 1st miss
    assert wd.observe(1, None) is False  # 2nd miss, dropped


def test_unit_recovers_once_heartbeat_resumes():
    wd = HeartbeatWatchdog(stall_cycles=2)
    wd.observe(1, 1)
    wd.observe(1, 1)  # stalled
    assert wd.observe(1, 1) is False  # dropped
    assert wd.observe(1, 2) is True   # heartbeat moved again, back in


def test_dead_unit_does_not_affect_others_state():
    """Unit1's heartbeat never moves (dead from the start). Unit2's counter
    increments normally every cycle. The two units' states must be fully
    independent: unit2 stays alive the whole time regardless of unit1.
    """
    wd = HeartbeatWatchdog(stall_cycles=2)
    for i in range(5):
        wd.observe(1, 100)  # dead unit, same value every cycle
        assert wd.observe(2, i) is True  # live unit, unaffected

    assert wd.is_alive(1) is False
    assert wd.is_alive(2) is True
