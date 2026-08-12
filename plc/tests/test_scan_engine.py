"""Tests for the PLC scan engine: model + PID + interlocks + mode watchdog.

These exercise ScanEngine directly (no OPC UA), it is the pure-Python core of
plc/unit.py, see plc/README.md for how the OPC UA server was verified
separately with a live asyncua client.

Levels here are in the two-tank model's real operating band (roughly
0-0.20 m, matching reference/water_mpc/mpc_core.py's H2_MAX=20 cm), not the
old single-tank model's meter-scale defaults.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plc.scan_engine import ScanEngine, SP_STALE_TIMEOUT_S

DT = 0.1  # one 100 ms scan, TIME_SCALE = 1.0


def make_engine(**overrides) -> ScanEngine:
    cfg = dict(hh=0.18, ll=0.01, local_sp=0.10, initial_level_m=0.05, initial_mode="AUTO")
    cfg.update(overrides)
    return ScanEngine(**cfg)


def test_pid_holds_setpoint_under_step_change():
    engine = make_engine(initial_level_m=0.05, local_sp=0.05)

    # Run to steady state at the initial setpoint. The tank's own time
    # constant at this scale (small orifices, small cross-section) is on
    # the order of a couple hundred simulated seconds, not the couple of
    # minutes the old meter-scale single-tank model needed.
    for _ in range(2500):
        out = engine.scan(DT, dcs_sp=0.05, dcs_sp_age_s=None)
    assert abs(out.level_pv - 0.05) < 0.01

    # Step the local setpoint up and run long enough to settle.
    engine.local_sp = 0.12
    for _ in range(3500):
        out = engine.scan(DT, dcs_sp=0.05, dcs_sp_age_s=None)

    assert abs(out.level_pv - 0.12) < 0.01, f"level did not settle near new SP: {out.level_pv}"
    assert not out.interlock_trip


def test_hh_interlock_trips_pump():
    # Start already above HH so the very first scan trips.
    engine = make_engine(hh=0.18, ll=0.01, initial_level_m=0.19, local_sp=0.10)

    out = engine.scan(DT, dcs_sp=0.10, dcs_sp_age_s=None)

    assert out.interlock_trip is True
    assert out.interlock_reason == "HH level"
    assert out.pump_cmd == 0.0
    assert out.pump_running is False

    # Trip is latched: even if level were to drop, it stays tripped until reset.
    engine.model.h1_cm = 10.0
    engine.model.h2_cm = 10.0
    out2 = engine.scan(DT, dcs_sp=0.10, dcs_sp_age_s=None)
    assert out2.interlock_trip is True

    # Manual reset clears it.
    engine.reset_interlock()
    out3 = engine.scan(DT, dcs_sp=0.10, dcs_sp_age_s=None)
    assert out3.interlock_trip is False


def test_stale_pid_sp_drops_cascade_to_auto():
    engine = make_engine(initial_mode="CASCADE", local_sp=0.10, initial_level_m=0.10)

    # A few healthy scans in CASCADE tracking a fresh DCS setpoint.
    for _ in range(10):
        out = engine.scan(DT, dcs_sp=0.15, dcs_sp_age_s=0.5)
    assert out.pid_mode == "CASCADE"
    assert engine._last_cascade_sp == 0.15

    # Now the DCS write goes stale (age exceeds the watchdog timeout).
    out = engine.scan(DT, dcs_sp=0.15, dcs_sp_age_s=SP_STALE_TIMEOUT_S + 1.0)

    assert out.pid_mode == "AUTO"
    assert engine.mode == "AUTO"
    assert engine.local_sp == 0.15  # falls back to the last known good setpoint
    assert out.level_sp == 0.15
