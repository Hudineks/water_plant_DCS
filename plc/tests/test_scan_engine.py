"""Tests for the PLC scan engine: model + PID + interlocks + mode watchdog.

These exercise ScanEngine directly (no OPC UA), it is the pure-Python core of
plc/unit.py, see plc/README.md for how the OPC UA server was verified
separately with a live asyncua client.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plc.scan_engine import ScanEngine, SP_STALE_TIMEOUT_S

DT = 0.1  # one 100 ms scan, TIME_SCALE = 1.0


def make_engine(**overrides) -> ScanEngine:
    cfg = dict(hh=4.0, ll=0.3, local_sp=2.0, initial_level_m=1.5, initial_mode="AUTO")
    cfg.update(overrides)
    return ScanEngine(**cfg)


def test_pid_holds_setpoint_under_step_change():
    engine = make_engine(initial_level_m=1.0, local_sp=1.0)

    # Run to steady state at the initial setpoint.
    for _ in range(1500):
        out = engine.scan(DT, dcs_sp=1.0, dcs_sp_age_s=None)
    assert abs(out.level_pv - 1.0) < 0.05

    # Step the local setpoint up and run long enough to settle.
    engine.local_sp = 2.5
    for _ in range(6000):
        out = engine.scan(DT, dcs_sp=1.0, dcs_sp_age_s=None)

    assert abs(out.level_pv - 2.5) < 0.05, f"level did not settle near new SP: {out.level_pv}"
    assert not out.interlock_trip


def test_hh_interlock_trips_pump():
    # Start already above HH so the very first scan trips.
    engine = make_engine(hh=4.0, ll=0.3, initial_level_m=4.5, local_sp=2.0)

    out = engine.scan(DT, dcs_sp=2.0, dcs_sp_age_s=None)

    assert out.interlock_trip is True
    assert out.interlock_reason == "HH level"
    assert out.pump_cmd == 0.0
    assert out.pump_running is False

    # Trip is latched: even if level were to drop, it stays tripped until reset.
    engine.model.level_m = 2.0
    out2 = engine.scan(DT, dcs_sp=2.0, dcs_sp_age_s=None)
    assert out2.interlock_trip is True

    # Manual reset clears it.
    engine.reset_interlock()
    out3 = engine.scan(DT, dcs_sp=2.0, dcs_sp_age_s=None)
    assert out3.interlock_trip is False


def test_stale_pid_sp_drops_cascade_to_auto():
    engine = make_engine(initial_mode="CASCADE", local_sp=2.0, initial_level_m=2.0)

    # A few healthy scans in CASCADE tracking a fresh DCS setpoint.
    for _ in range(10):
        out = engine.scan(DT, dcs_sp=3.0, dcs_sp_age_s=0.5)
    assert out.pid_mode == "CASCADE"
    assert engine._last_cascade_sp == 3.0

    # Now the DCS write goes stale (age exceeds the watchdog timeout).
    out = engine.scan(DT, dcs_sp=3.0, dcs_sp_age_s=SP_STALE_TIMEOUT_S + 1.0)

    assert out.pid_mode == "AUTO"
    assert engine.mode == "AUTO"
    assert engine.local_sp == 3.0  # falls back to the last known good setpoint
    assert out.level_sp == 3.0
