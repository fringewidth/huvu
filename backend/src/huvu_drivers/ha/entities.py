"""Exposed Home Assistant entities as huvu actions.

Routines are HA scripts (scripts.py). Everything else huvu works directly:
each exposed entity in a domain below becomes one action of the source,
named after its domain and object id (`light.theatre_ceiling` becomes
`ha_light_theatre_ceiling`). Its arguments come from the entity's own
attributes when the driver reads HA (the modes a climate offers, a select's
options, a number's range), and a call becomes one or more HA service calls
on that entity alone.

Autonomy, the same labels as scripts (scripts.LEVELS):

1. A huvu label on the entity wins.
2. Else a huvu label on its area, so a whole room can ask first.
3. Else its domain's default (DEFAULTS).

Locks, alarms, and garage, gate and door covers always need a person's tap,
whatever their labels say (HUMAN).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from huvu.prompts import TEXT
from huvu.spec import MEMBER_RE, ActionSpec, Autonomy

from .scripts import LEVELS, strictest

# What a domain does without a label. Lights, switches and buttons are what
# people ask for many times a day, so they don't ask; a climate, a vacuum or
# a cover runs and tells the person; anything else asks.
DEFAULTS: dict[str, Autonomy] = {
    "light": "silent",
    "switch": "silent",
    "fan": "silent",
    "input_boolean": "silent",
    "button": "silent",
    "input_button": "silent",
    "scene": "silent",
    "media_player": "silent",
    "number": "silent",
    "input_number": "silent",
    "select": "silent",
    "input_select": "silent",
    "climate": "notify",
    "vacuum": "notify",
    "cover": "notify",
}
DEFAULT_LEVEL: Autonomy = "ask"
# Never without a person's tap, and no label lowers it.
HUMAN_DOMAINS = {"lock", "alarm_control_panel"}
HUMAN_COVERS = {"garage", "gate", "door"}

MEDIA_COMMANDS = {
    "turn_on": ("turn_on", {}),
    "turn_off": ("turn_off", {}),
    "play": ("media_play", {}),
    "pause": ("media_pause", {}),
    "stop": ("media_stop", {}),
    "next": ("media_next_track", {}),
    "previous": ("media_previous_track", {}),
    "volume_up": ("volume_up", {}),
    "volume_down": ("volume_down", {}),
    "mute": ("volume_mute", {"is_volume_muted": True}),
    "unmute": ("volume_mute", {"is_volume_muted": False}),
}
VACUUM_COMMANDS = ["start", "pause", "stop", "return_to_base", "locate"]
COVER_COMMANDS = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
RGB_MODES = {"rgb", "rgbw", "rgbww", "hs", "xy"}

Call = tuple[str, str, dict[str, Any]]


@dataclass
class Control:
    """What the driver keeps about one exposed entity huvu may work. Cached
    in the source's folder, so the actions exist before HA connects."""

    entity_id: str
    name: str
    input_schema: dict[str, Any]
    labels: list[str] = field(default_factory=list)
    area: str | None = None
    area_labels: list[str] = field(default_factory=list)
    device: str | None = None
    aliases: list[str] = field(default_factory=list)
    device_class: str | None = None
    # HA only assumes its state (infrared, no sensor).
    assumed: bool = False

    @property
    def domain(self) -> str:
        return self.entity_id.partition(".")[0]

    @property
    def action_name(self) -> str:
        return self.entity_id.replace(".", "_", 1)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def controllable(entity_id: str) -> bool:
    return entity_id.partition(".")[0] in SCHEMAS


def from_state(entity_id: str, state: dict[str, Any], *, labels: list[str], area: str | None, area_labels: list[str], device: str | None, aliases: list[str]) -> Control:
    attributes = state.get("attributes") or {}
    return Control(
        entity_id=entity_id,
        name=str(attributes.get("friendly_name") or entity_id),
        input_schema=SCHEMAS[entity_id.partition(".")[0]](attributes),
        labels=sorted(labels),
        area=area,
        area_labels=sorted(area_labels),
        device=device,
        aliases=list(aliases),
        device_class=attributes.get("device_class"),
        assumed=bool(attributes.get("assumed_state")),
    )


def problems_of(control: Control) -> list[str]:
    """What is wrong with an entity's setup. A problem that starts with
    "left out" means the entity is not an action."""
    found: list[str] = []
    if not MEMBER_RE.match(control.action_name):
        found.append(f"left out {control.entity_id}: its id can't be an action name")
    for label in control.labels:
        if label.startswith("huvu") and label not in LEVELS:
            found.append(f"{control.entity_id} has the label {label!r}, which isn't one of {', '.join(LEVELS)}")
    return found


def usable(control: Control) -> bool:
    return not any(p.startswith("left out") for p in problems_of(control))


def level(control: Control) -> tuple[Autonomy, bool]:
    """The entity's huvu label, else its area's, else the domain's default.
    A lock, an alarm or a garage, gate or door cover always needs a tap."""
    human = control.domain in HUMAN_DOMAINS or (control.domain == "cover" and control.device_class in HUMAN_COVERS)
    found = strictest(control.labels) or strictest(control.area_labels) or (DEFAULTS.get(control.domain, DEFAULT_LEVEL), False)
    return ("ask", True) if human else found


def action(control: Control) -> ActionSpec:
    autonomy, human = level(control)
    return ActionSpec(name=control.action_name, description=describe(control), input_schema=control.input_schema, autonomy=autonomy, requires_human=human)


def describe(control: Control) -> str:
    parts = [f"{control.name} ({control.entity_id}).", TEXT[f"ha.entity.{control.domain}"]]
    if control.area:
        parts.append(TEXT["ha.entity.area"].format(area=control.area))
    if control.device and control.device != control.name:
        parts.append(TEXT["ha.entity.device"].format(device=control.device))
    if control.aliases:
        parts.append(TEXT["ha.entity.aliases"].format(aliases=", ".join(control.aliases)))
    if control.assumed:
        parts.append(TEXT["ha.entity.assumed"])
    return " ".join(parts)


def service_calls(control: Control, args: dict[str, Any]) -> list[Call]:
    """The HA service calls an action's arguments become, in order. Raises
    ValueError when the arguments don't fit the action."""
    check(control.input_schema, args)
    return CALLS[control.domain](control.domain, args)


# Arguments, per domain, from the entity's attributes


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _arg(kind: str, key: str, **more: Any) -> dict[str, Any]:
    return {"type": kind, "description": TEXT[f"ha.arg.{key}"], **more}


def _choice(key: str, options: Any) -> dict[str, Any]:
    values = [str(o) for o in options or []]
    return _arg("string", key, enum=values) if values else _arg("string", key)


def _range(kind: str, key: str, low: Any, high: Any) -> dict[str, Any]:
    bounds = {k: v for k, v in (("minimum", low), ("maximum", high)) if isinstance(v, int | float)}
    return _arg(kind, key, **bounds)


def _on_off(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"on": _arg("boolean", "on")}, ["on"])


def _light(attributes: dict[str, Any]) -> dict[str, Any]:
    modes = set(attributes.get("supported_color_modes") or [])
    properties: dict[str, Any] = {"on": _arg("boolean", "on")}
    if modes != {"onoff"}:
        properties["brightness_pct"] = _range("integer", "brightness_pct", 1, 100)
    if "color_temp" in modes:
        properties["color_temp_kelvin"] = _range("integer", "color_temp_kelvin", attributes.get("min_color_temp_kelvin"), attributes.get("max_color_temp_kelvin"))
    if modes & RGB_MODES:
        properties["rgb_color"] = _arg("array", "rgb_color", items={"type": "integer", "minimum": 0, "maximum": 255})
    return _schema(properties, ["on"])


def _fan(attributes: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {"on": _arg("boolean", "on"), "percentage": _range("integer", "percentage", 0, 100)}
    if attributes.get("preset_modes"):
        properties["preset_mode"] = _choice("preset_mode", attributes["preset_modes"])
    return _schema(properties, ["on"])


def _nothing(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({})


def _media(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"command": _choice("media_command", MEDIA_COMMANDS), "volume": _range("integer", "volume", 0, 100)})


def _climate(attributes: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "hvac_mode": _choice("hvac_mode", attributes.get("hvac_modes")),
        "temperature": _range("number", "temperature", attributes.get("min_temp"), attributes.get("max_temp")),
    }
    if attributes.get("fan_modes"):
        properties["fan_mode"] = _choice("fan_mode", attributes["fan_modes"])
    return _schema(properties)


def _vacuum(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"command": _choice("vacuum_command", VACUUM_COMMANDS)}, ["command"])


def _number(attributes: dict[str, Any]) -> dict[str, Any]:
    value = _range("number", "value", attributes.get("min"), attributes.get("max"))
    if isinstance(attributes.get("step"), int | float):
        value["multipleOf"] = attributes["step"]
    return _schema({"value": value}, ["value"])


def _select(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"option": _choice("option", attributes.get("options"))}, ["option"])


def _cover(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"command": _choice("cover_command", COVER_COMMANDS), "position": _range("integer", "position", 0, 100)})


def _lock(attributes: dict[str, Any]) -> dict[str, Any]:
    return _schema({"command": _choice("lock_command", ["lock", "unlock"])}, ["command"])


SCHEMAS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "light": _light,
    "switch": _on_off,
    "input_boolean": _on_off,
    "fan": _fan,
    "button": _nothing,
    "input_button": _nothing,
    "scene": _nothing,
    "media_player": _media,
    "climate": _climate,
    "vacuum": _vacuum,
    "number": _number,
    "input_number": _number,
    "select": _select,
    "input_select": _select,
    "cover": _cover,
    "lock": _lock,
}


# Arguments to service calls, per domain


def _picked(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: args[k] for k in keys if k in args}


def _needs_one(args: dict[str, Any], *keys: str) -> None:
    if not any(k in args for k in keys):
        raise ValueError(f"give at least one of {', '.join(keys)}")


def _light_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    on = [(domain, "turn_on", _picked(args, "brightness_pct", "color_temp_kelvin", "rgb_color"))]
    return on if args["on"] else [(domain, "turn_off", {})]


def _on_off_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, "turn_on" if args["on"] else "turn_off", {})]


def _fan_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    on = [(domain, "turn_on", _picked(args, "percentage", "preset_mode"))]
    return on if args["on"] else [(domain, "turn_off", {})]


def _press_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, "turn_on" if domain == "scene" else "press", {})]


def _media_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    _needs_one(args, "command", "volume")
    calls: list[Call] = []
    if "command" in args:
        service, data = MEDIA_COMMANDS[args["command"]]
        calls.append((domain, service, data))
    if "volume" in args:
        calls.append((domain, "volume_set", {"volume_level": args["volume"] / 100}))
    return calls


def _climate_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    _needs_one(args, "hvac_mode", "temperature", "fan_mode")
    calls: list[Call] = []
    # set_temperature takes the mode too, so both go in one call.
    if "temperature" in args:
        calls.append((domain, "set_temperature", _picked(args, "temperature", "hvac_mode")))
    elif "hvac_mode" in args:
        calls.append((domain, "set_hvac_mode", _picked(args, "hvac_mode")))
    if "fan_mode" in args:
        calls.append((domain, "set_fan_mode", _picked(args, "fan_mode")))
    return calls


def _vacuum_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, args["command"], {})]


def _number_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, "set_value", {"value": args["value"]})]


def _select_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, "select_option", {"option": args["option"]})]


def _cover_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    _needs_one(args, "command", "position")
    calls: list[Call] = [(domain, COVER_COMMANDS[args["command"]], {})] if "command" in args else []
    if "position" in args:
        calls.append((domain, "set_cover_position", {"position": args["position"]}))
    return calls


def _lock_calls(domain: str, args: dict[str, Any]) -> list[Call]:
    return [(domain, args["command"], {})]


CALLS: dict[str, Callable[[str, dict[str, Any]], list[Call]]] = {
    "light": _light_calls,
    "switch": _on_off_calls,
    "input_boolean": _on_off_calls,
    "fan": _fan_calls,
    "button": _press_calls,
    "input_button": _press_calls,
    "scene": _press_calls,
    "media_player": _media_calls,
    "climate": _climate_calls,
    "vacuum": _vacuum_calls,
    "number": _number_calls,
    "input_number": _number_calls,
    "select": _select_calls,
    "input_select": _select_calls,
    "cover": _cover_calls,
    "lock": _lock_calls,
}


# Checking arguments against the schema an action declared

TYPES: dict[str, Callable[[Any], bool]] = {
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
}


def check(schema: dict[str, Any], args: dict[str, Any]) -> None:
    """Raises ValueError naming the first argument that doesn't fit."""
    properties = schema.get("properties") or {}
    unknown = [k for k in args if k not in properties]
    missing = [k for k in schema.get("required") or [] if k not in args]
    problems = [f"takes no {k}" for k in unknown] + [f"needs {k}" for k in missing]
    problems += [p for k, v in args.items() if k in properties for p in _value_problems(k, properties[k], v)]
    if problems:
        raise ValueError("; ".join(problems))


def _value_problems(name: str, spec: dict[str, Any], value: Any) -> list[str]:
    kind = spec.get("type", "string")
    found: list[str] = []
    if not TYPES.get(kind, lambda v: True)(value):
        found.append(f"{name} must be a{'n' if kind[0] in 'aeiou' else ''} {kind}")
    elif "enum" in spec and value not in spec["enum"]:
        found.append(f"{name} must be one of {', '.join(spec['enum'])}")
    elif kind in ("integer", "number") and ("minimum" in spec and value < spec["minimum"] or "maximum" in spec and value > spec["maximum"]):
        found.append(f"{name} must be from {spec.get('minimum', '-inf')} to {spec.get('maximum', 'inf')}")
    elif kind == "array" and "items" in spec:
        found.extend(p for item in value for p in _value_problems(f"each of {name}", spec["items"], item))
    return found
