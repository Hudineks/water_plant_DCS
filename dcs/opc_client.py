"""OPC UA client side of the DCS: one connection per PLC unit, with
subscriptions on the tags the control loop needs every cycle (Level.PV,
Status.Heartbeat) and automatic reconnect on disconnect.

Each UnitClient owns its own asyncua Client and keeps a local cache of the
latest subscribed values. The control loop (main.py) reads this cache
synchronously each cycle instead of awaiting a fresh read, so a slow or
stalled PLC never blocks the whole cycle; a stale/missing cache value shows
up to the watchdog as a stalled heartbeat.
"""
from __future__ import annotations

import asyncio
import logging

from asyncua import Client
from asyncua.common.subscription import Subscription

from plantbus.client import read_all, resolve_unit_nodes
from plantbus.contract import UNIT_TAGS

logger = logging.getLogger("dcs.opc_client")

SUBSCRIBED_TAGS = ["Level.PV", "Status.Heartbeat"]
SUBSCRIPTION_PERIOD_MS = 250


class _SubHandler:
    def __init__(self, cache: dict[str, object], node_to_tag: dict, unit_id: int):
        self._cache = cache
        self._node_to_tag = node_to_tag
        self._unit_id = unit_id

    def datachange_notification(self, node, val, data):
        tag = self._node_to_tag.get(node)
        if tag is not None:
            self._cache[tag] = val

    def event_notification(self, event):
        pass


class UnitClient:
    """Owns the connection to one PLC unit. Call run() as a background task;
    it never returns under normal operation, it reconnects internally.
    """

    def __init__(self, unit_id: int, endpoint: str, reconnect_backoff_s: float):
        self.unit_id = unit_id
        self.endpoint = endpoint
        self.reconnect_backoff_s = reconnect_backoff_s

        self.connected = False
        self._client: Client | None = None
        self._nodes: dict = {}
        self._subscription: Subscription | None = None
        self._cache: dict[str, object] = {}
        self._stop = False

    async def run(self) -> None:
        while not self._stop:
            try:
                await self._connect_and_subscribe()
                # asyncua keeps the connection alive via its own background
                # tasks; block here until something breaks the connection.
                while not self._stop:
                    await asyncio.sleep(1.0)
                    await self._client.check_connection()
            except Exception as exc:
                logger.warning("Unit%d: connection lost or failed (%s), retrying in %.1fs",
                                self.unit_id, exc, self.reconnect_backoff_s)
                self.connected = False
                await self._safe_disconnect()
                await asyncio.sleep(self.reconnect_backoff_s)

    async def stop(self) -> None:
        self._stop = True
        await self._safe_disconnect()

    async def _safe_disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self.connected = False

    async def _connect_and_subscribe(self) -> None:
        client = Client(url=self.endpoint)
        await client.connect()
        self._client = client

        objects = client.get_objects_node()
        unit_object = None
        for child in await objects.get_children():
            browse_name = await child.read_browse_name()
            if browse_name.Name == f"Unit{self.unit_id}":
                unit_object = child
                break
        if unit_object is None:
            raise RuntimeError(f"Unit{self.unit_id} object node not found on {self.endpoint}")

        self._nodes = await resolve_unit_nodes(unit_object, UNIT_TAGS)

        node_to_tag = {self._nodes[tag]: tag for tag in SUBSCRIBED_TAGS if tag in self._nodes}
        handler = _SubHandler(self._cache, node_to_tag, self.unit_id)
        self._subscription = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, handler)
        await self._subscription.subscribe_data_change([self._nodes[t] for t in SUBSCRIBED_TAGS if t in self._nodes])

        # Seed the cache with an initial read so the control loop has values
        # before the first subscription notification arrives.
        seed = await read_all({t: self._nodes[t] for t in SUBSCRIBED_TAGS if t in self._nodes})
        self._cache.update(seed)

        self.connected = True
        logger.info("Unit%d: connected to %s", self.unit_id, self.endpoint)

    def get_cached(self, tag: str):
        return self._cache.get(tag)

    async def read_snapshot(self) -> dict[str, object]:
        """Full read of every tag, for the historian. Best-effort: returns
        an empty dict if the unit is currently disconnected.
        """
        if not self.connected or not self._nodes:
            return {}
        try:
            return await read_all(self._nodes)
        except Exception:
            return {}

    async def write_pid_sp(self, value_m: float) -> None:
        if not self.connected or "PID.SP" not in self._nodes:
            raise RuntimeError(f"Unit{self.unit_id} not connected, cannot write PID.SP")
        await self._nodes["PID.SP"].write_value(float(value_m))

    async def read_bounds(self) -> tuple[float, float]:
        """Return (Level.LL, Level.HH) in meters, defaults if unavailable."""
        try:
            ll = await self._nodes["Level.LL"].read_value()
            hh = await self._nodes["Level.HH"].read_value()
            return float(ll), float(hh)
        except Exception:
            return 0.0, 9.0
