"""The DCS's own OPC UA server, exposing the global tags from tags.yaml
(APC.Enabled, APC.SolveTime_ms, APC.Status), plus a per-unit diagnostic
node that is NOT part of the frozen tags.yaml contract.

Why the DCS runs a server at all: tags.yaml documents the global group as
"live on the DCS's own server-side status, consumed by the HMI". The DCS is
already an OPC UA client of the N PLC units; it also runs a small server of
its own so the HMI (or any other OPC UA client) can read APC.SolveTime_ms /
APC.Status and write APC.Enabled without needing a channel into the DCS
process besides OPC UA. This keeps the whole stack on one protocol instead
of adding a REST/websocket side channel for three tags.

Diagnostics.H1_Estimated: the real rig's upstream tank (h1) has no sensor,
only an EKF estimate computed alongside the MPC (reference/water_mpc's
x_hat[0,0]). The real rig never exposes this over any wire protocol either,
so it does not belong in tags.yaml's unit contract (which describes what a
PLC actually publishes). It is genuinely useful for the HMI to show,
though, so the DCS publishes it itself, per unit, under
Unit{n}/Diagnostics/H1_Estimated on this server -- built directly here with
a small ad hoc helper rather than routed through plantbus/tags.yaml.
"""
from __future__ import annotations

from asyncua import Server, ua
from asyncua.common.node import Node

from plantbus.contract import GLOBAL_TAGS
from plantbus.server import build_unit_nodes

DCS_SERVER_URI = "http://water-plant-dcs/dcs"


class GlobalServer:
    def __init__(self, endpoint: str, unit_ids: list[int] | None = None):
        self.endpoint = endpoint
        self.unit_ids = unit_ids or []
        self._server: Server | None = None
        self.nodes: dict[str, Node] = {}
        self._diagnostic_nodes: dict[int, Node] = {}

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

        for unit_id in self.unit_ids:
            unit_object = await objects.add_object(idx, f"Unit{unit_id}")
            diag_object = await unit_object.add_object(idx, "Diagnostics")
            node = await diag_object.add_variable(idx, "H1_Estimated", 0.0)
            await node.set_writable(False)
            self._diagnostic_nodes[unit_id] = node

        await self._server.start()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()

    async def read_enabled(self) -> bool:
        return bool(await self.nodes["APC.Enabled"].read_value())

    async def publish_status(self, solve_time_ms: float, status: str) -> None:
        await self.nodes["APC.SolveTime_ms"].write_value(float(solve_time_ms))
        await self.nodes["APC.Status"].write_value(status)

    async def publish_diagnostics(self, unit_id: int, h1_estimated_m: float) -> None:
        node = self._diagnostic_nodes.get(unit_id)
        if node is not None:
            await node.write_value(float(h1_estimated_m))
