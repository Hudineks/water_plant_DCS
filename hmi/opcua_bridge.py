"""Background OPC UA polling for the HMI.

Connects to every PLC unit endpoint and, separately, to the DCS endpoint for
the global APC.* tags. Polls on a fixed interval (no subscriptions, this is
an operator display, not a control loop) and keeps recent history for
trending. All state lives in PlantState, which the FastAPI websocket layer
reads and pushes to browsers.

If a PLC or the DCS is unreachable, the corresponding poll loop keeps retrying
with a short backoff and marks that source as disconnected. One dead unit
must not take down the display of the other units.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asyncua import Client
from asyncua.ua.uaerrors import UaError

from plantbus.client import read_all, resolve_unit_nodes
from plantbus.contract import GLOBAL_TAGS, UNIT_TAGS

logger = logging.getLogger("hmi.opcua_bridge")

POLL_PERIOD_S = 1.0
RECONNECT_BACKOFF_S = 3.0
HISTORY_WINDOW_S = 10 * 60  # 10 minutes of PV/SP trend, per plan item 1
HEARTBEAT_STALE_S = 5.0  # no heartbeat increment within this window => unit considered dead


@dataclass
class UnitState:
    unit_id: int
    endpoint: str
    connected: bool = False
    values: dict = field(default_factory=dict)
    last_heartbeat: object = None
    last_heartbeat_change: float = 0.0
    history: deque = field(default_factory=deque)  # (t, Level.PV, Level.SP, cycle_name)
    error: str = ""

    @property
    def alive(self) -> bool:
        """Connected and heartbeat still advancing (PLC scan not frozen)."""
        if not self.connected:
            return False
        if self.last_heartbeat_change == 0.0:
            return True  # just connected, no heartbeat history yet
        return (time.monotonic() - self.last_heartbeat_change) < HEARTBEAT_STALE_S

    def snapshot(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "connected": self.connected,
            "alive": self.alive,
            "values": self.values,
            "error": self.error,
            "history": list(self.history),
        }


@dataclass
class DcsState:
    endpoint: str
    connected: bool = False
    values: dict = field(default_factory=dict)
    # unit_id -> h1 estimate (m). Not part of tags.yaml, see
    # dcs/global_server.py's Unit{n}/Diagnostics/H1_Estimated node: the
    # real rig's upstream tank has no sensor, this is the DCS's own EKF
    # estimate, published diagnostically for the operator panel only.
    diagnostics: dict = field(default_factory=dict)
    # unit_id -> {"cycle_name": str, "manual_target_m": float}. Also not in
    # tags.yaml, see dcs/global_server.py's Unit{n}/Control/CycleName and
    # Control/ManualTargetM: the operator-writable per-unit setpoint
    # source selection (off/step/ramp/manual).
    unit_control: dict = field(default_factory=dict)
    error: str = ""

    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "values": self.values,
            "diagnostics": self.diagnostics,
            "unit_control": self.unit_control,
            "error": self.error,
        }


class PlantState:
    """Holds the latest readings from all units and the DCS, shared with the
    websocket broadcaster. write_unit_setpoint / write_apc_enabled are called
    from the FastAPI request handlers when the operator acts on the panel.
    """

    def __init__(self, unit_endpoints: list[str], dcs_endpoint: str):
        self.units = {
            i + 1: UnitState(unit_id=i + 1, endpoint=ep)
            for i, ep in enumerate(unit_endpoints)
        }
        self.dcs = DcsState(endpoint=dcs_endpoint)
        self._unit_write_nodes: dict[int, dict] = {}
        self._dcs_write_nodes: dict = {}
        self._unit_control_write_nodes: dict[int, dict] = {}
        self.on_change = None  # set by main.py to trigger a websocket push

    def snapshot(self) -> dict:
        return {
            "units": {uid: u.snapshot() for uid, u in self.units.items()},
            "dcs": self.dcs.snapshot(),
            "server_time": time.time(),
        }

    async def write_unit_setpoint(self, unit_id: int, value: float):
        nodes = self._unit_write_nodes.get(unit_id)
        if not nodes or "PID.SP" not in nodes:
            raise RuntimeError(f"Unit{unit_id} not connected, cannot write setpoint")
        await nodes["PID.SP"].write_value(float(value))

    async def write_apc_enabled(self, enabled: bool):
        node = self._dcs_write_nodes.get("APC.Enabled")
        if node is None:
            raise RuntimeError("DCS not connected, cannot write APC.Enabled")
        await node.write_value(bool(enabled))

    async def write_unit_cycle(self, unit_id: int, cycle_name: str, target_m: float | None = None):
        """Sets a unit's live setpoint source (dcs/global_server.py's
        Control.CycleName), and its manual target when given. dcs/main.py
        picks this up within one control cycle (~1 s) and applies it,
        including a bumpless reset if the source actually changed."""
        nodes = self._unit_control_write_nodes.get(unit_id)
        if not nodes:
            raise RuntimeError(f"Unit{unit_id} control not connected, cannot set cycle")
        await nodes["CycleName"].write_value(str(cycle_name))
        if target_m is not None:
            await nodes["ManualTargetM"].write_value(float(target_m))


async def _find_child_object(client: Client, label: str):
    objects = client.get_objects_node()
    for child in await objects.get_children():
        bn = await child.read_browse_name()
        if bn.Name == label:
            return child
    raise RuntimeError(f"No object node named {label} on server")


async def _find_unit_object(client: Client, unit_id: int):
    return await _find_child_object(client, f"Unit{unit_id}")


async def _poll_unit(state: PlantState, unit_id: int):
    unit = state.units[unit_id]
    while True:
        try:
            async with Client(url=unit.endpoint, timeout=4) as client:
                unit_object = await _find_unit_object(client, unit_id)
                nodes = await resolve_unit_nodes(unit_object, UNIT_TAGS)
                state._unit_write_nodes[unit_id] = nodes
                unit.connected = True
                unit.error = ""
                logger.info("Unit%d connected at %s", unit_id, unit.endpoint)

                while True:
                    values = await read_all(nodes)
                    now = time.monotonic()

                    hb = values.get("Status.Heartbeat")
                    if hb != unit.last_heartbeat:
                        unit.last_heartbeat = hb
                        unit.last_heartbeat_change = now

                    unit.values = values
                    cycle_name = state.dcs.unit_control.get(unit_id, {}).get("cycle_name", "off")
                    unit.history.append((time.time(), values.get("Level.PV"), values.get("Level.SP"), cycle_name))
                    cutoff = time.time() - HISTORY_WINDOW_S
                    while unit.history and unit.history[0][0] < cutoff:
                        unit.history.popleft()

                    if state.on_change:
                        state.on_change()

                    await asyncio.sleep(POLL_PERIOD_S)

        except (UaError, OSError, asyncio.TimeoutError, ConnectionError) as exc:
            unit.connected = False
            unit.error = str(exc) or type(exc).__name__
            state._unit_write_nodes.pop(unit_id, None)
            logger.warning("Unit%d disconnected (%s), retrying in %ss", unit_id, unit.error, RECONNECT_BACKOFF_S)
            if state.on_change:
                state.on_change()
            await asyncio.sleep(RECONNECT_BACKOFF_S)


async def _poll_dcs(state: PlantState):
    dcs = state.dcs
    while True:
        try:
            async with Client(url=dcs.endpoint, timeout=4) as client:
                global_object = await _find_child_object(client, "Global")
                nodes = await resolve_unit_nodes(global_object, GLOBAL_TAGS)
                state._dcs_write_nodes = nodes
                dcs.connected = True
                dcs.error = ""
                logger.info("DCS connected at %s", dcs.endpoint)

                diag_nodes: dict[int, object] = {}
                control_read_nodes: dict[int, dict] = {}
                for unit_id in state.units:
                    try:
                        unit_object = await _find_child_object(client, f"Unit{unit_id}")
                        idx = unit_object.nodeid.NamespaceIndex

                        diag_object = await unit_object.get_child(f"{idx}:Diagnostics")
                        diag_nodes[unit_id] = await diag_object.get_child(f"{idx}:H1_Estimated")

                        control_object = await unit_object.get_child(f"{idx}:Control")
                        cycle_node = await control_object.get_child(f"{idx}:CycleName")
                        target_node = await control_object.get_child(f"{idx}:ManualTargetM")
                        control_read_nodes[unit_id] = {"CycleName": cycle_node, "ManualTargetM": target_node}
                        state._unit_control_write_nodes[unit_id] = control_read_nodes[unit_id]
                    except UaError:
                        pass  # diagnostic/control nodes not published for this unit, skip it

                while True:
                    dcs.values = await read_all(nodes)
                    dcs.diagnostics = {
                        uid: await node.read_value() for uid, node in diag_nodes.items()
                    }
                    dcs.unit_control = {
                        uid: {
                            "cycle_name": await cn["CycleName"].read_value(),
                            "manual_target_m": await cn["ManualTargetM"].read_value(),
                        }
                        for uid, cn in control_read_nodes.items()
                    }
                    if state.on_change:
                        state.on_change()
                    await asyncio.sleep(POLL_PERIOD_S)

        except (UaError, OSError, asyncio.TimeoutError, ConnectionError) as exc:
            dcs.connected = False
            dcs.error = str(exc) or type(exc).__name__
            state._dcs_write_nodes = {}
            state._unit_control_write_nodes = {}
            logger.warning("DCS disconnected (%s), retrying in %ss", dcs.error, RECONNECT_BACKOFF_S)
            if state.on_change:
                state.on_change()
            await asyncio.sleep(RECONNECT_BACKOFF_S)


def start_polling(state: PlantState) -> list[asyncio.Task]:
    tasks = [asyncio.create_task(_poll_unit(state, uid)) for uid in state.units]
    tasks.append(asyncio.create_task(_poll_dcs(state)))
    return tasks
