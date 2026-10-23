"""Persistent model-selected time/weather, with scene-local particles/events."""

from background_effects import scene_effect_context


WEATHER_FIELDS = frozenset({"weather", "intensity", "temperature", "season", "climate", "humidity"})
LOCAL_FIELDS = frozenset({"ambient", "ambient_source", "ambient_intensity"})


def persistent_conditions(value):
    clean = scene_effect_context(value)
    weather = {key: item for key, item in clean.get("atmosphere", {}).items() if key in WEATHER_FIELDS}
    result = {"time_of_day": clean["time_of_day"]} if "time_of_day" in clean else {}
    if weather:
        result["atmosphere"] = weather
    return result


def initial_conditions(plan):
    value = plan.get("initial_conditions") if isinstance(plan, dict) else None
    return persistent_conditions(value)


def advance_conditions(previous, scene):
    """Missing/invalid delta fields hold state; local effects require a new request.

    A scene carrying conditions_change uses the compact contract. Legacy scenes
    with complete time/atmosphere fields remain explicit updates for old logs.
    """
    scene = scene if isinstance(scene, dict) else {}
    result = persistent_conditions(previous)
    delta = persistent_conditions(scene.get("conditions_change") if "conditions_change" in scene else scene)
    if "time_of_day" in delta:
        result["time_of_day"] = delta["time_of_day"]
    weather = dict(result.get("atmosphere", {}))
    changes = delta.get("atmosphere", {})
    weather.update(changes)
    if changes.get("weather") == "clear" and "intensity" not in changes:
        weather["intensity"] = 0.0
    # Do not inherit a previous fire/source or emitters, even at the same place.
    local = scene_effect_context(scene).get("atmosphere", {})
    weather.update({key: value for key, value in local.items() if key in LOCAL_FIELDS})
    if weather:
        result["atmosphere"] = weather
    return result
