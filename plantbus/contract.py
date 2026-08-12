"""Loader for tags.yaml, the single contract shared by plc/, dcs/, and hmi/.

Every OPC UA node in this project is derived from tags.yaml. This module
turns that file into plain Tag objects. It does not touch OPC UA itself,
see server.py and client.py for that.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml

TAGS_YAML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tags.yaml")

_TYPE_DEFAULTS = {
    "float": 0.0,
    "bool": False,
    "uint": 0,
    "string": "",
}


@dataclass(frozen=True)
class Tag:
    name: str          # e.g. "Level.PV"
    type: str          # float | bool | uint | string
    access: str         # R | RW
    unit: str | None = None
    description: str = ""
    enum: tuple[str, ...] | None = None

    @property
    def writable(self) -> bool:
        return self.access == "RW"

    @property
    def default(self) -> Any:
        if self.enum:
            return self.enum[0]
        return _TYPE_DEFAULTS[self.type]

    @property
    def node_path(self) -> list[str]:
        """Split 'Level.PV' into ['Level', 'PV'] for nested object nodes."""
        return self.name.split(".")


def _parse_tags(raw_tags: dict) -> list[Tag]:
    tags = []
    for tag_name, spec in raw_tags.items():
        tags.append(
            Tag(
                name=tag_name,
                type=spec["type"],
                access=spec.get("access", "R"),
                unit=spec.get("unit"),
                description=spec.get("description", "").strip(),
                enum=tuple(spec["enum"]) if "enum" in spec else None,
            )
        )
    return tags


def load_contract(path: str = TAGS_YAML_PATH) -> tuple[list[Tag], list[Tag]]:
    """Return (unit_tags, global_tags) parsed from tags.yaml."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    unit_tags = _parse_tags(raw["unit"]["tags"])
    global_tags = _parse_tags(raw["global"]["tags"])
    return unit_tags, global_tags


UNIT_TAGS, GLOBAL_TAGS = load_contract()
