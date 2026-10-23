"""Applicability of scene effects to a specific background, never auto-activation."""

from __future__ import annotations

import math
import hashlib
import json


# Lighting alone cannot tell an open deck from a covered carriage or a planet
# with unknown atmosphere. New backgrounds stay disabled until classified.
OPEN = set("""forest_path forest_clearing country_road meadow riverbank lakeshore
sandy_beach rocky_coast mountain_pass swamp old_bridge desert camp
settlement_street narrow_alley courtyard town_square market park embankment
harbor_quay cemetery ancient_ruins small_boat rooftop road_checkpoint
sailing_ship_deck metal_ship_deck railway_platform fuel_station
castle_courtyard castle_wall future_city""".split())
WINDOWED = set("""living_small bedroom kitchen office library_archive dining_room
corridor stairwell temple assembly_hall workshop stable prison_cell
tent_interior ship_cabin reception_hall abandoned_room train_car station_hall
car_interior bus_interior carriage_interior airport_terminal hospital_room
laboratory factory_floor museum hangar mage_study""".split())
SEALED = set("""cave basement warehouse forge mine underground_tunnel ship_hold
metro_car underground_parking elevator operating_room server_room theater""".split())
SPACE = {"space_station_corridor", "spaceship_bridge", "space_observation_deck"}
FIREFLIES = {"forest_path", "forest_clearing", "meadow", "riverbank", "lakeshore", "swamp", "camp", "park"}
FIRE_SITES = {"camp", "forge"}
SPARK_SITES = {"forge", "workshop", "factory_floor", "server_room", "laboratory", "hangar"}
DUST_SITES = {"cave", "basement", "warehouse", "library_archive", "abandoned_room", "mine", "underground_tunnel", "ancient_ruins"}
# Current artwork is green and snow-free. Falling snow is only approved for
# backgrounds where a brief snowfall does not require repainting vegetation.
SNOW_SITES = set("""rocky_coast mountain_pass settlement_street narrow_alley
town_square embankment harbor_quay rooftop road_checkpoint sailing_ship_deck
metal_ship_deck railway_platform fuel_station castle_wall future_city""".split()) | WINDOWED
KNOWN = OPEN | WINDOWED | SEALED | SPACE | {"airplane_cabin", "alien_planet", "abstract_space"}


def _polygon(value) -> list[list[float]] | None:
    """Normalized coordinates refer to the image, not the viewport."""
    if not isinstance(value, list) or not 3 <= len(value) <= 16:
        return None
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
            return None
        points.append([float(v) for v in point])
    area = abs(sum(points[i][0] * points[(i + 1) % len(points)][1] -
                   points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))) / 2
    return points if area > .0001 else None


def effect_profile_for(item: dict) -> dict:
    asset_id = item.get("id") or item.get("asset_id")
    fallback = item.get("is_fallback") or asset_id == "abstract_space"
    if fallback:
        setting = "fallback"
    elif asset_id in OPEN:
        setting = "open"
    elif asset_id in WINDOWED:
        setting = "sheltered"
    elif asset_id in SEALED:
        setting = "sealed"
    elif asset_id in SPACE:
        setting = "space"
    elif asset_id == "airplane_cabin":
        setting = "aircraft"
    elif asset_id == "alien_planet":
        setting = "alien"
    else:
        setting = "unknown"
    supplied = item.get("effect_profile")
    supplied = supplied if isinstance(supplied, dict) else {}
    artwork_changed = bool(supplied.get("artwork_sha256") and item.get("sha256") and
                           supplied["artwork_sha256"] != item["sha256"])
    # Artwork-specific masks/emitters must be reviewed after image regeneration.
    masks = supplied.get("weather_masks")
    masks = [_polygon(mask) for mask in masks[:8]] if isinstance(masks, list) else []
    masks = [mask for mask in masks if mask]
    regions = supplied.get("effect_regions")
    regions = regions if isinstance(regions, dict) else {}
    regions = {key: polygon for key, value in regions.items()
               if key in {"fireflies", "embers", "sparks", "dust_motes"} and (polygon := _polygon(value))}
    if artwork_changed:
        masks, regions = [], {}
    weather = ["rain", "storm", "fog"] if setting in {"open", "sheltered"} else []
    if setting in {"open", "sheltered"} and asset_id in SNOW_SITES:
        weather.append("snow")
    if asset_id == "desert":
        weather = ["rain", "storm"]
    return {
        "schema_version": 1, "setting": setting,
        "artwork_sha256": item.get("sha256") or supplied.get("artwork_sha256"),
        "weather_mode": "full" if setting == "open" else "masked" if setting == "sheltered" else "none",
        "weather_allowed": weather, "weather_masks": masks,
        "fireflies_habitat": bool(asset_id in FIREFLIES and not fallback),
        "ash_fallout": setting == "open",
        "local_effects": [name for name, sites in (("embers", FIRE_SITES), ("sparks", SPARK_SITES), ("dust_motes", DUST_SITES))
                          if asset_id in sites and not fallback],
        "effect_regions": regions,
        "event_flash": setting not in {"unknown", "fallback"},
    }


def _intensity(value, default: float = .5) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return round(max(0.0, min(1.0, value)), 3)


def scene_effect_fields(scene: dict) -> dict:
    """Keep optional render metadata when raw prose is materialized or restored."""
    result = {}
    time = scene.get("time_of_day")
    if time in ("morning", "day", "evening", "night"):
        result["time_of_day"] = time
    for field, kind in (("atmosphere", dict), ("screen_events", list)):
        value = scene.get(field)
        if isinstance(value, kind):
            result[field] = value
    return result


def scene_effect_context(scene: dict) -> dict:
    """Bounded model-selected conditions for the next generation, never events."""
    if not isinstance(scene, dict):
        return {}
    result = {}
    time = scene.get("time_of_day")
    if time in ("morning", "day", "evening", "night"):
        result["time_of_day"] = time
    atmosphere = scene.get("atmosphere")
    if isinstance(atmosphere, dict):
        allowed = {
            "weather": {"clear", "rain", "storm", "snow", "fog"},
            "temperature": {"warm", "cold", "unknown"},
            "season": {"spring", "summer", "autumn", "winter", "unknown"},
            "climate": {"temperate", "cold", "alpine", "tropical", "hot_arid", "unknown"},
            "humidity": {"humid", "dry", "unknown"},
            "ambient": {"none", "fireflies", "ash", "embers", "sparks", "dust_motes"},
            "ambient_source": {"none", "wildfire", "volcano", "large_fire", "explosion",
                               "fire", "metalworking", "electrical_fault", "disturbed_dust"},
        }
        clean = {}
        for key, choices in allowed.items():
            value = atmosphere.get(key)
            if isinstance(value, str) and value in choices:
                clean[key] = value
        for key in ("intensity", "ambient_intensity"):
            value = atmosphere.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                clean[key] = _intensity(value)
        if clean:
            result["atmosphere"] = clean
    return result


def effect_scene_key(session_id: str, node_id: str | None, scene: dict) -> str:
    identity = [session_id, node_id] if node_id else [session_id, scene]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def resolve_scene_effects(scene: dict, background: dict | None, *, time_of_day: str = "day", genre: str = "") -> dict:
    """Return approved requests and explicit rejection reasons for diagnostics.

    Capability is not a request. Plain narration/quotes never activate effects.
    """
    profile = effect_profile_for(background or {})
    atmosphere = scene.get("atmosphere")
    atmosphere = atmosphere if isinstance(atmosphere, dict) else {}
    weather = atmosphere.get("weather", "clear")
    ambient = atmosphere.get("ambient", "none")
    weather = weather if isinstance(weather, str) else "invalid"
    ambient = ambient if isinstance(ambient, str) else "invalid"
    temperature = atmosphere.get("temperature", "unknown")
    temperature = temperature if isinstance(temperature, str) else "unknown"
    season = atmosphere.get("season", "unknown")
    season = season if isinstance(season, str) else "unknown"
    climate = atmosphere.get("climate", "unknown")
    climate = climate if isinstance(climate, str) else "unknown"
    source = atmosphere.get("ambient_source", "none")
    source = source if isinstance(source, str) else "none"
    result = {"weather": None, "ambient": None, "events": [], "blocked": [], "grading": None}
    # Window light may change without precipitation entering the room. Actual
    # particles still require reviewed window masks. Fixed light never changes.
    background = background or {}
    grade_allowed = (profile["setting"] in {"open", "sheltered"}
                     and not background.get("is_fallback")
                     and background.get("time_mode") != "fixed"
                     and background.get("lighting_profile") not in {"fixed", "enclosed", "space"})
    grading = {"strength": 1.0 if profile["setting"] == "open" else .22,
               "season": None, "weather": None, "ambient": None}
    if (grade_allowed and (background.get("asset_id") or background.get("id")) in SNOW_SITES
            and temperature == "cold" and season == "winter" and climate in {"temperate", "cold", "alpine"}):
        grading["season"] = "winter"

    def block(effect, reason):
        result["blocked"].append({"effect": effect, "reason": reason})

    blocked_weather = False
    if weather not in {"clear", "none"}:
        reason = None
        if weather not in profile["weather_allowed"]:
            reason = "weather_not_allowed_on_background"
        elif weather == "snow" and temperature != "cold":
            reason = "snow_requires_cold"
        elif weather == "snow" and climate not in {"temperate", "cold", "alpine"}:
            reason = "snow_requires_compatible_climate"
        elif weather == "snow" and season not in {"winter", "spring", "autumn"} and not (season == "summer" and climate == "alpine"):
            reason = "snow_requires_compatible_season"
        elif weather == "fog" and atmosphere.get("humidity") != "humid":
            reason = "fog_requires_humidity"
        if not reason and grade_allowed:
            grading["weather"] = {"type": weather, "intensity": _intensity(atmosphere.get("intensity"))}
        if not reason and profile["weather_mode"] == "masked" and not profile["weather_masks"]:
            reason = "window_masks_required"
        if reason:
            block(weather, reason)
            blocked_weather = True
        else:
            result["weather"] = {"type": weather, "intensity": _intensity(atmosphere.get("intensity")),
                                 "mode": profile["weather_mode"], "masks": profile["weather_masks"]}
    if ambient not in {"none", "clear"}:
        reason = None
        if ambient == "fireflies":
            if not profile["fireflies_habitat"]:
                reason = "fireflies_habitat_required"
            elif time_of_day not in {"evening", "night"}:
                reason = "fireflies_require_evening_or_night"
            elif temperature != "warm" or season not in {"spring", "summer", "autumn"}:
                reason = "fireflies_require_warm_known_season"
            elif climate not in {"temperate", "tropical"}:
                reason = "fireflies_require_compatible_climate"
            elif weather not in {"clear", "none"}:
                reason = "fireflies_require_calm_weather"
        elif ambient == "ash":
            if not profile["ash_fallout"]:
                reason = "ash_requires_open_air"
            elif source not in {"wildfire", "volcano", "large_fire", "explosion"}:
                reason = "ash_requires_explicit_source"
            elif weather not in {"clear", "none", "fog"}:
                reason = "ash_conflicts_with_precipitation"
        elif ambient not in profile["local_effects"]:
            reason = "ambient_not_allowed_on_background"
        if not reason and ambient in {"embers", "sparks", "dust_motes"} and source not in {
            "embers": {"fire"}, "sparks": {"metalworking", "electrical_fault"}, "dust_motes": {"disturbed_dust"}
        }[ambient]:
            reason = "ambient_requires_explicit_source"
        if not reason and ambient != "ash" and ambient not in profile["effect_regions"]:
            reason = "effect_region_required"
        if reason:
            block(ambient, reason)
        else:
            result["ambient"] = {"type": ambient, "intensity": _intensity(atmosphere.get("ambient_intensity"), .3),
                                 "source": source, "region": profile["effect_regions"].get(ambient)}

    if grade_allowed:
        if result["ambient"] and result["ambient"]["type"] == "ash":
            grading["ambient"] = {"type": "ash", "intensity": result["ambient"]["intensity"]}
        if grading["season"] or grading["weather"] or grading["ambient"]:
            result["grading"] = grading

    events = scene.get("screen_events")
    for event in events[:4] if isinstance(events, list) else []:
        if not isinstance(event, dict):
            block("invalid", "invalid_event")
            continue
        effect, source = event.get("type"), event.get("source")
        if effect != "flash" or source not in ("lightning", "explosion", "camera", "magic", "impact"):
            block(str(effect)[:40], "explicit_supported_source_required")
        elif not profile["event_flash"]:
            block("flash", "flash_not_allowed_on_background")
        elif source == "lightning" and (weather != "storm" or blocked_weather):
            block("flash", "lightning_requires_visible_storm")
        elif source == "magic" and genre.casefold() not in {"фэнтези", "фентези", "мистика", "fantasy"}:
            block("flash", "magic_requires_compatible_genre")
        elif result["events"]:
            block("flash", "one_flash_per_scene")
        else:
            result["events"].append({"type": "flash", "source": source,
                "intensity": min(.55, _intensity(event.get("intensity"), .3)),
                "mode": profile["weather_mode"] if source == "lightning" else "full",
                "masks": profile["weather_masks"] if source == "lightning" else []})
    return result
