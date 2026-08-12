"""Builds an OPC UA server address space for one PLC unit from tags.yaml.

Usage:
    from asyncua import Server
    from plantbus.server import build_unit_nodes

    server = Server()
    ...
    nodes = await build_unit_nodes(server, unit_object)
    await nodes["Level.PV"].write_value(1.23)
"""
from __future__ import annotations

from asyncua import ua
from asyncua.common.node import Node

from .contract import UNIT_TAGS, Tag

_UA_TYPE = {
    "float": ua.VariantType.Double,
    "bool": ua.VariantType.Boolean,
    "uint": ua.VariantType.UInt32,
    "string": ua.VariantType.String,
}


async def build_unit_nodes(server, parent, tags: list[Tag] = UNIT_TAGS) -> dict[str, Node]:
    """Create one OPC UA variable per tag under `parent`, grouped by the
    dotted prefix (Level.PV and Level.SP share an object node "Level").

    Returns a flat dict keyed by the full tag name, e.g. "Level.PV".
    """
    idx = parent.nodeid.NamespaceIndex
    group_nodes: dict[str, Node] = {}
    flat_nodes: dict[str, Node] = {}

    for tag in tags:
        group_name, leaf_name = tag.node_path
        if group_name not in group_nodes:
            group_nodes[group_name] = await parent.add_object(idx, group_name)
        group = group_nodes[group_name]

        variant_type = _UA_TYPE[tag.type]
        var = await group.add_variable(idx, leaf_name, tag.default, varianttype=variant_type)
        if tag.writable:
            await var.set_writable()
        flat_nodes[tag.name] = var

    return flat_nodes
