"""Helpers for OPC UA clients (dcs/, hmi/) reading a unit's address space.

The node layout on the server is Unit/<Group>/<Leaf>, grouped by the dotted
prefix in tags.yaml (Level.PV and Level.SP live under object node "Level").
This module only knows how to walk that layout, it does not manage the
client connection lifecycle, callers own that.
"""
from __future__ import annotations

from asyncua import Node

from .contract import UNIT_TAGS, Tag


async def resolve_unit_nodes(unit_object: Node, tags: list[Tag] = UNIT_TAGS) -> dict[str, Node]:
    """Browse a unit's object node and return {tag_name: Node} for every tag."""
    flat_nodes: dict[str, Node] = {}
    group_cache: dict[str, Node] = {}

    for tag in tags:
        group_name, leaf_name = tag.node_path
        if group_name not in group_cache:
            group_cache[group_name] = await unit_object.get_child(f"{unit_object.nodeid.NamespaceIndex}:{group_name}")
        group = group_cache[group_name]
        leaf = await group.get_child(f"{group.nodeid.NamespaceIndex}:{leaf_name}")
        flat_nodes[tag.name] = leaf

    return flat_nodes


async def read_all(nodes: dict[str, Node]) -> dict[str, object]:
    """Read every node in the dict, return {tag_name: value}."""
    values = [await n.read_value() for n in nodes.values()]
    return dict(zip(nodes.keys(), values))
