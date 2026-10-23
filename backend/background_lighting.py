"""Lighting policy shared by retrieval and the asset importer."""

TIME_MODES = {"outdoor", "windowed", "fixed"}


def time_mode_for(item: dict) -> str:
    explicit = item.get("time_mode")
    if explicit in TIME_MODES:
        return explicit
    if item.get("is_fallback") or item.get("id") in {"abstract_space", "metro_car"}:
        return "fixed"
    if item.get("id") == "hangar":
        return "windowed"
    environment = item.get("environment") or item.get("lighting")
    if environment in {"enclosed", "space", "fixed"}:
        return "fixed"
    return "windowed" if environment == "windowed" else "outdoor"


def fallback_definition() -> dict:
    return {
        "id": "abstract_space", "label": "Нейтральная подложка",
        "description": "An abstract continuous digital gouache color field, broad soft matte shapes in muted slate gray, dusty taupe and desaturated blue, subtle hand-painted texture and gentle tonal depth. No recognizable room, architecture, objects, landscape, horizon, sun, sky or directional lighting. The painting fills the entire frame with calm balanced shapes and a simple foreground.",
        "lighting": "enclosed", "environment": "enclosed", "time_mode": "fixed",
        "group": "fallback", "is_fallback": True, "file": None,
    }
