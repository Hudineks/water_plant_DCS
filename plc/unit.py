"""Real (simulated) PLC unit: OPC UA server driving a scan-cycle loop.

One process = one tank unit. Run as Unit1, Unit2, ... by setting UNIT_ID.

Usage:
    UNIT_ID=1 OPCUA_PORT=4840 python -m plc.unit
    UNIT_ID=2 OPCUA_PORT=4841 python -m plc.unit

See plc/README.md for the full list of environment variables and the scan
structure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asyncua import Server, ua

from plantbus.contract import UNIT_TAGS
from plantbus.server import build_unit_nodes

from plc.scan_engine import ScanEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("plc.unit")

SCAN_PERIOD_S = 0.1  # 100 ms scan cycle, real wall-clock cadence


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _load_config() -> dict:
    unit_id = int(os.environ.get("UNIT_ID", "1"))
    return dict(
        unit_id=unit_id,
        port=int(os.environ.get("OPCUA_PORT", "4840")),
        time_scale=_env_float("TIME_SCALE", 1.0),
        hh=_env_float("LEVEL_HH", 4.0),
        ll=_env_float("LEVEL_LL", 0.3),
        local_sp=_env_float("UNIT_LOCAL_SP", 2.0),
        valve_cmd_pct=_env_float("VALVE_CMD_PCT", 30.0),
        pid_kp=_env_float("PID_KP", 40.0),
        pid_ki=_env_float("PID_KI", 5.0),
        pid_kd=_env_float("PID_KD", 0.0),
        area_m2=_env_float("TANK_AREA_M2", 2.0),
        pump_max_flow_m3s=_env_float("PUMP_MAX_FLOW_M3S", 0.05),
        valve_cv=_env_float("VALVE_CV", 0.03),
        initial_level_m=_env_float("INITIAL_LEVEL_M", 1.5),
        initial_mode=os.environ.get("UNIT_INITIAL_MODE", "CASCADE"),
        reset_file=os.environ.get(
            "RESET_FILE", f"./unit{os.environ.get('UNIT_ID', '1')}.reset"
        ),
    )


async def run(cfg: dict) -> None:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{cfg['port']}/")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    uri = "http://water-plant-dcs/plc"
    idx = await server.register_namespace(uri)

    objects = server.get_objects_node()
    unit_object = await objects.add_object(idx, f"Unit{cfg['unit_id']}")

    nodes = await build_unit_nodes(server, unit_object, UNIT_TAGS)

    engine = ScanEngine(
        hh=cfg["hh"],
        ll=cfg["ll"],
        local_sp=cfg["local_sp"],
        valve_cmd_pct=cfg["valve_cmd_pct"],
        pid_kp=cfg["pid_kp"],
        pid_ki=cfg["pid_ki"],
        pid_kd=cfg["pid_kd"],
        area_m2=cfg["area_m2"],
        pump_max_flow_m3s=cfg["pump_max_flow_m3s"],
        valve_cv=cfg["valve_cv"],
        initial_level_m=cfg["initial_level_m"],
        initial_mode=cfg["initial_mode"],
    )

    # Seed static/initial values so a client connecting before the first scan
    # completes still sees sane numbers.
    await nodes["Level.HH"].write_value(engine.interlock.hh)
    await nodes["Level.LL"].write_value(engine.interlock.ll)
    await nodes["Interlock.Reason"].write_value("")
    await nodes["PID.SP"].write_value(engine.local_sp)
    await nodes["PID.Mode"].write_value(engine.mode)

    reset_path = Path(cfg["reset_file"])
    dt_sim = SCAN_PERIOD_S * cfg["time_scale"]

    logger.info(
        "Unit%d serving on opc.tcp://0.0.0.0:%d/ (time_scale=%.2f, dt_sim=%.3fs, reset_file=%s)",
        cfg["unit_id"], cfg["port"], cfg["time_scale"], dt_sim, reset_path,
    )

    async with server:
        while True:
            loop_t0 = time.perf_counter()

            # --- READ INPUTS (from OPC UA, i.e. from the DCS) ---
            sp_dv = await nodes["PID.SP"].read_data_value()
            dcs_sp = sp_dv.Value.Value
            sp_ts = sp_dv.SourceTimestamp or sp_dv.ServerTimestamp
            if sp_ts is not None:
                now_utc = datetime.now(timezone.utc)
                sp_age_s = (now_utc - sp_ts).total_seconds()
            else:
                sp_age_s = None

            if reset_path.exists():
                engine.reset_interlock()
                reset_path.unlink()

            # --- EXECUTE LOGIC + integrate physics ---
            out = engine.scan(dt_sim, dcs_sp, sp_age_s)

            # --- WRITE OUTPUTS ---
            await nodes["Level.PV"].write_value(out.level_pv)
            await nodes["Level.SP"].write_value(out.level_sp)
            await nodes["Level.HH"].write_value(out.level_hh)
            await nodes["Level.LL"].write_value(out.level_ll)
            await nodes["Pump.CMD"].write_value(out.pump_cmd)
            await nodes["Pump.FB"].write_value(out.pump_fb)
            await nodes["Pump.Running"].write_value(out.pump_running)
            await nodes["Valve.CMD"].write_value(out.valve_cmd)
            await nodes["Valve.FB"].write_value(out.valve_fb)
            await nodes["PID.OUT"].write_value(out.pid_out)
            await nodes["PID.Mode"].write_value(out.pid_mode)
            await nodes["Interlock.Trip"].write_value(out.interlock_trip)
            await nodes["Interlock.Reason"].write_value(out.interlock_reason)
            await nodes["Status.Heartbeat"].write_value(ua.Variant(out.heartbeat, ua.VariantType.UInt32))
            await nodes["Status.ScanTime_ms"].write_value(out.scan_time_ms)

            elapsed = time.perf_counter() - loop_t0
            sleep_s = max(0.0, SCAN_PERIOD_S - elapsed)
            await asyncio.sleep(sleep_s)


def main() -> None:
    cfg = _load_config()
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
