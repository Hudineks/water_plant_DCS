"""The DCS's own OPC UA server, exposing the global tags from tags.yaml
(APC.Enabled, APC.SolveTime_ms, APC.Status).

Why the DCS runs a server at all: tags.yaml documents the global group as
"live on the DCS's own server-side status, consumed by the HMI". The DCS is
already an OPC UA client of the N PLC units; it also runs a small server of
its own so the HMI (or any other OPC UA client) can read APC.SolveTime_ms /
APC.Status and write APC.Enabled without needing a channel into the DCS
process besides OPC UA. This keeps the whole stack on one protocol instead
of adding a REST/websocket side channel for three tags.
"""
from __future__ import annotations

from asyncua import Server, ua
from asyncua.common.node import Node

from plantbus.contract import GLOBAL_TAGS
from plantbus.server import build_unit_nodes

DCS_SERVER_URI = "http://water-plant-dcs/dcs"


class GlobalServer:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._server: Server | None = None
        self.nodes: dict[str, Node] = {}

    async def start(self) -> None:
        self._server = Server()
        await self._server.init()
        self._server.set_endpoint(self.endpoint)
        self._server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

        idx = await self._server.register_namespace(DCS_SERVER_URI)
        objects = self._server.get_objects_node()
        # build_unit_nodes only cares about tag.node_path grouping, it
        # works the same for the global tag list.
        global_object = await objects.add_object(idx, "Global")
        self.nodes = await build_unit_nodes(self._server, global_object, GLOBAL_TAGS)

        await self.nodes["APC.Enabled"].write_value(False)
        await self.nodes["APC.SolveTime_ms"].write_value(0.0)
        await self.nodes["APC.Status"].write_value("DISABLED")

        await self._server.start()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()

    async def read_enabled(self) -> bool:
        return bool(await self.nodes["APC.Enabled"].read_value())

    async def publish_status(self, solve_time_ms: float, status: str) -> None:
        await self.nodes["APC.SolveTime_ms"].write_value(float(solve_time_ms))
        await self.nodes["APC.Status"].write_value(status)
