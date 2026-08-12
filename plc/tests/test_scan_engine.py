"""Tests for the PLC scan engine: model + flow-to-Pump.CMD conversion +
interlocks + mode watchdog.

These exercise ScanEngine directly (no OPC UA), it is the pure-Python core
of plc/unit.py, see plc/README.md for how the OPC UA server was verified
separately with a live asyncua client.

PID.SP is a flow (cm3/s), not a level, and CASCADE/AUTO's conversion to
Pump.CMD is a static linear map (no PID, no settling time) -- see
scan_engine.py's module docstring for why. Levels here are in the
two-tank model's real operating band (roughly 0-0.20 m, matching
reference/water_mpc/mpc_core.py's H2_MAX=20 cm).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plc.scan_engine import ScanEngine, SP_STALE_TIMEOUT_S

DT = 0.1  # one 100 ms scan, TIME_SCALE = 1.0


def make_engine(**overrides) -> ScanEngine:
    cfg = dict(hh=0.18, ll=0.01, initial_level_m=0.05, initial_mode="AUTO")
    cfg.update(overrides)
    return ScanEngine(**cfg)


def test_cascade_flow_converts_linearly_to_pump_cmd():
    engine = make_engine(initial_mode="CASCADE")
    max_flow = engine.model.pump_max_flow_cm3s

    out = engine.scan(DT, dcs_sp=max_flow / 2.0, dcs_sp_age_s=0.5)
    assert out.pump_cmd == 50.0
    assert out.pid_out == 50.0

    out = engine.scan(DT, dcs_sp=max_flow, dcs_sp_age_s=0.5)
    assert out.pump_cmd == 100.0

    # A flow request beyond the actuator's own max clips to 100%, not an
    # out-of-range percentage.
    out = engine.scan(DT, dcs_sp=max_flow * 2.0, dcs_sp_age_s=0.5)
    assert out.pump_cmd == 100.0


def test_level_converges_to_torricelli_steady_state_under_cascade_flow():
    engine = make_engine(initial_mode="CASCADE", initial_level_m=0.05)
    flow_cm3s = 8.5

    for _ in range(3000):
        out = engine.scan(DT, dcs_sp=flow_cm3s, dcs_sp_age_s=0.5)

    # Torricelli balance at steady state: K_OUT * sqrt(h2_cm) == flow.
    from plc.model import K_OUT

    expected_h2_cm = (flow_cm3s / K_OUT) ** 2
    assert abs(out.level_pv - expected_h2_cm / 100.0) < 0.002, (
        f"level {out.level_pv:.4f} m did not converge near the expected "
        f"Torricelli steady state {expected_h2_cm / 100.0:.4f} m"
    )
    assert not out.interlock_trip


def test_hh_interlock_trips_pump():
    # Start already above HH so the very first scan trips.
    engine = make_engine(hh=0.18, ll=0.01, initial_level_m=0.19)

    out = engine.scan(DT, dcs_sp=10.0, dcs_sp_age_s=None)

    assert out.interlock_trip is True
    assert out.interlock_reason == "HH level"
    assert out.pump_cmd == 0.0
    assert out.pump_running is False

    # Trip is latched: even if level were to drop, it stays tripped until reset.
    engine.model.h1_cm = 10.0
    engine.model.h2_cm = 10.0
    out2 = engine.scan(DT, dcs_sp=10.0, dcs_sp_age_s=None)
    assert out2.interlock_trip is True

    # Manual reset clears it.
    engine.reset_interlock()
    out3 = engine.scan(DT, dcs_sp=10.0, dcs_sp_age_s=None)
    assert out3.interlock_trip is False


def test_stale_pid_sp_drops_cascade_to_auto_and_zeros_flow():
    engine = make_engine(initial_mode="CASCADE", initial_level_m=0.10)

    # A few healthy scans in CASCADE tracking a fresh DCS setpoint.
    for _ in range(10):
        out = engine.scan(DT, dcs_sp=8.5, dcs_sp_age_s=0.5)
    assert out.pid_mode == "CASCADE"
    assert out.pump_cmd > 0.0

    # Now the DCS write goes stale (age exceeds the watchdog timeout). The
    # fail-safe is zero flow, not holding whatever was last commanded.
    out = engine.scan(DT, dcs_sp=8.5, dcs_sp_age_s=SP_STALE_TIMEOUT_S + 1.0)

    assert out.pid_mode == "AUTO"
    assert engine.mode == "AUTO"
    assert out.pump_cmd == 0.0
    assert out.pump_running is False

    # Stays zero on subsequent scans too, even though dcs_sp is still
    # nonzero -- AUTO ignores it entirely once tripped.
    out2 = engine.scan(DT, dcs_sp=8.5, dcs_sp_age_s=SP_STALE_TIMEOUT_S + 2.0)
    assert out2.pump_cmd == 0.0
