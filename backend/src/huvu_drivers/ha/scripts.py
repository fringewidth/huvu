"""Home Assistant scripts as huvu actions.

The household writes its routines in HA as scripts, each with a
description, fields and an area, exposed to Assist. Each exposed script
becomes one action of this source, named after the script's object id
(`script.bedroom_ac_set` becomes `ha_bedroom_ac_set`). Single devices are
worked directly (entities.py).

What the script says in HA becomes what the model sees:

1. Its `description` is the action's description, with its area.
2. Its `fields` become the input schema: each field's selector becomes a
   JSON Schema type (SELECTORS below), its `required` the required list.
3. Its labels set the policy's autonomy level (LEVELS). A script without a
   huvu label asks first, so a forgotten label costs a tap, never an
   unasked action.

Anything that doesn't fit is a setup problem: logged, listed in the
`ha_setup` table, and, where it can't work at all, left out.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from huvu.prompts import TEXT
from huvu.spec import MEMBER_RE, ActionSpec, Autonomy

# Label id -> (autonomy, requires_human). HA makes a label's id from its
# name, so a label named "huvu_ask" has the id huvu_ask.
LEVELS: dict[str, tuple[Autonomy, bool]] = {
    "huvu_silent": ("silent", False),
    "huvu_notify": ("notify", False),
    "huvu_ask": ("ask", False),
    "huvu_human": ("ask", True),
}
DEFAULT_LEVEL: Autonomy = "ask"

ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


@dataclass
class Script:
    """What the driver keeps about one exposed script. Cached in the
    source's folder, so the actions exist before HA connects."""

    entity_id: str
    object_id: str
    name: str
    description: str
    fields: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    area: str | None = None
    # Exposed entities the script's config names, so a search for the AC
    # also offers the scripts that work it.
    targets: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def from_config(entity_id: str, name: str, config: dict[str, Any], labels: list[str], area: str | None, exposed: set[str]) -> Script:
    object_id = entity_id.partition(".")[2]
    targets = sorted({e for e in _strings(config.get("sequence")) if e in exposed and e != entity_id})
    return Script(
        entity_id=entity_id,
        object_id=object_id,
        name=config.get("alias") or name,
        description=" ".join(str(config.get("description") or "").split()),
        fields=dict(config.get("fields") or {}),
        labels=sorted(labels),
        area=area,
        targets=targets,
    )


def problems_of(script: Script) -> list[str]:
    """What is wrong with a script's setup. A problem that starts with
    "left out" means the script is not an action."""
    found: list[str] = []
    if not MEMBER_RE.match(script.object_id):
        found.append(f"left out {script.entity_id}: its id must be lower case letters, digits and underscores, starting with a letter")
    if not script.description:
        found.append(f"{script.entity_id} has no description; the model only sees its name")
    levels = [label for label in script.labels if label in LEVELS]
    if len(levels) > 1:
        found.append(f"{script.entity_id} has several huvu labels ({', '.join(levels)}); the strictest wins")
    for label in script.labels:
        if label.startswith("huvu") and label not in LEVELS:
            found.append(f"{script.entity_id} has the label {label!r}, which isn't one of {', '.join(LEVELS)}")
    for name, spec in script.fields.items():
        kind = next(iter((spec or {}).get("selector") or {}), None)
        if kind is None:
            found.append(f"{script.entity_id}: field {name!r} has no selector, so the model gets no type for it")
        elif kind not in SELECTORS:
            found.append(f"{script.entity_id}: field {name!r} has a {kind} selector; the model gets it as free text")
        if kind in ("entity", "area", "device", "target"):
            found.append(f"{script.entity_id}: field {name!r} lets the caller pick any {kind}; prefer one script per device, or a select of fixed options")
    return found


def usable(script: Script) -> bool:
    return not any(p.startswith("left out") for p in problems_of(script))


def strictest(labels: list[str]) -> tuple[Autonomy, bool] | None:
    """The level of the strictest huvu label among these, or None."""
    order = ["huvu_human", "huvu_ask", "huvu_notify", "huvu_silent"]
    found = next((label for label in order if label in labels), None)
    return LEVELS[found] if found else None


def level(script: Script) -> tuple[Autonomy, bool]:
    """The strictest huvu label wins; none means ask."""
    return strictest(script.labels) or (DEFAULT_LEVEL, False)


def action(script: Script) -> ActionSpec:
    autonomy, human = level(script)
    description = script.description or script.name
    if script.area:
        description = f"{description.rstrip('.')}. Area: {script.area}."
    return ActionSpec(name=script.object_id, description=description, input_schema=schema(script.fields), autonomy=autonomy, requires_human=human)


def schema(fields: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in fields.items():
        spec = spec or {}
        prop = selector_schema(spec.get("selector") or {})
        notes = [str(spec.get("description") or spec.get("name") or "").strip()]
        if "example" in spec:
            notes.append(f"Example: {spec['example']}.")
        if "default" in spec:
            notes.append(f"Default: {spec['default']}.")
        note = " ".join(n for n in notes if n)
        if note:
            prop["description"] = f"{prop['description']} {note}" if prop.get("description") else note
        properties[name] = prop
        if spec.get("required"):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _select(config: dict[str, Any]) -> dict[str, Any]:
    options = [o["value"] if isinstance(o, dict) else o for o in config.get("options") or []]
    one: dict[str, Any] = {"type": "string", "enum": [str(o) for o in options]} if options else {"type": "string"}
    return {"type": "array", "items": one} if config.get("multiple") else one


def _number(config: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "number"}
    if "min" in config:
        out["minimum"] = config["min"]
    if "max" in config:
        out["maximum"] = config["max"]
    unit = config.get("unit_of_measurement")
    if unit:
        out["description"] = f"In {unit}."
    return out


SELECTORS = {
    "number": _number,
    "select": _select,
    "boolean": lambda c: {"type": "boolean"},
    "text": lambda c: {"type": "string"},
    "time": lambda c: {"type": "string", "description": TEXT["ha.arg.time"]},
    "date": lambda c: {"type": "string", "description": TEXT["ha.arg.date"]},
    "duration": lambda c: {"type": "object", "properties": {"hours": {"type": "number"}, "minutes": {"type": "number"}, "seconds": {"type": "number"}}},
    "color_temp": lambda c: {"type": "number", "description": TEXT["ha.arg.color_temp.kelvin"] if c.get("unit") == "kelvin" else TEXT["ha.arg.color_temp.mireds"]},
    "color_rgb": lambda c: {"type": "array", "items": {"type": "integer"}, "description": TEXT["ha.arg.color_rgb"]},
}


def selector_schema(selector: dict[str, Any]) -> dict[str, Any]:
    kind, config = next(iter(selector.items()), (None, None))
    build = SELECTORS.get(kind or "")
    return build(config or {}) if build else {"type": "string"}


def _strings(node: Any) -> list[str]:
    """Every string in a script's sequence that looks like an entity id."""
    found: list[str] = []
    if isinstance(node, str):
        found.extend(s.strip() for s in node.split(",") if ENTITY_ID_RE.match(s.strip()))
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_strings(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_strings(value))
    return found
