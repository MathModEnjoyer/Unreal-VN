"""Stable story places; modern events own transitions, legacy prose uses quotes."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from scene_conditions import initial_conditions, advance_conditions

ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
MAX_PLACES = 256


def _text(value, limit=160):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _key(value):
    return unicodedata.normalize("NFKC", _text(value)).casefold().replace("ё", "е")


def place_id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else ""


def _generated_id(name):
    return "place_" + hashlib.sha256(_key(name).encode("utf-8")).hexdigest()[:16]


def load_places(value):
    """Copy only bounded, supported save fields; never share branch dictionaries."""
    result = {}
    if isinstance(value, dict):
        for key, entry in list(value.items())[:MAX_PLACES]:
            if not place_id(key) or not isinstance(entry, dict) or not _text(entry.get("name")):
                continue
            result[key] = {"name": _text(entry["name"])}
            background = place_id(entry.get("background_id"))
            if background:
                result[key]["background_id"] = background
    return result


def ensure_current_place(state):
    current = place_id(state.location_id)
    if current in state.locations:
        state.location = state.locations[current]["name"]
        return current
    name = _text(state.location)
    if not name:
        state.location_id = ""
        return ""
    current = next((key for key, entry in state.locations.items() if _key(entry["name"]) == _key(name)), _generated_id(name))
    state.locations.setdefault(current, {"name": name})
    state.location_id = current
    state.location = state.locations[current]["name"]
    return current


def _transition(scene):
    event = scene.get('_planned_event')
    if isinstance(event, dict) and event.get('v') in (2, 3):
        location = event.get('location')
        if isinstance(location, dict) and location.get('transition') in ('move', 'cut'):
            return {'kind': location['transition']}
        return None
    transition = scene.get("location_transition")
    if not isinstance(transition, dict) or transition.get("kind") not in ("move", "cut"):
        return None
    evidence = _text(transition.get("evidence"), 301)
    narration = " ".join(scene.get("narration", "").split()) if isinstance(scene.get("narration"), str) else ""
    # Ground the assertion in current narration, never dialogue or a choice.
    if not 8 <= len(evidence) <= 300 or evidence not in narration:
        return None
    return {"kind": transition["kind"], "evidence": evidence}


def _bind_background(entry, requested):
    # A saved ID is stable even if art is temporarily unavailable.
    if entry.get('background_id') or not isinstance(requested, str):
        return
    if place_id(requested):
        entry['background_id'] = requested


def apply_scene_location(state, scene, *, legacy=False):
    current = ensure_current_place(state)
    name = _text(scene.get("location"))
    requested = place_id(scene.get("location_id"))
    transition = _transition(scene)
    issue = None
    if requested in state.locations:
        target = requested
    elif name:
        # Repeated exact names also converge when a model invents another ID.
        target = next((key for key, entry in state.locations.items() if _key(entry["name"]) == _key(name)), requested or _generated_id(name))
    else:
        target = current
    if "location_id" in scene and scene["location_id"] is not None and not requested:
        issue = "invalid_location_id"
    elif current and target != current and not legacy and not transition:
        issue = "location_transition_required"
    elif target and target not in state.locations and len(state.locations) >= MAX_PLACES:
        issue = "location_registry_full"
    if issue:
        target = current
    if target and target not in state.locations:
        state.locations[target] = {"name": name}
    changed = target != current
    state.location_id = target
    if target:
        entry = state.locations[target]
        state.location = entry["name"]
        if not issue:
            _bind_background(entry, scene.get("location_background_id"))
    result = {"location": state.location, "location_id": target,
              "location_background_id": state.locations.get(target, {}).get("background_id"),
              "location_transition": transition if changed and not issue else None}
    if issue:
        result["location_issue"] = issue
    return result


def restore_location_path(state, tree, node_id):
    """Restore locations and conditions from this branch's ancestors only."""
    root = state.tree_root_id
    if root not in tree or node_id not in tree:
        return
    parents, pending = {root: None}, [root]
    while pending and node_id not in parents:
        current = pending.pop()
        node = tree[current]
        children = node.get("children") or node.get("segment_children_ids") or {}
        for child in children.values():
            if child in tree and child not in parents:
                parents[child] = current
                pending.append(child)
    if node_id not in parents:
        return
    ancestors, current = [], parents[node_id]
    while current is not None:
        ancestors.append(current)
        current = parents[current]
    state.locations, state.location_id, state.location = {}, "", ""
    state.scene_conditions = initial_conditions(state.story_plan)
    for ancestor in reversed(ancestors):
        scene = tree[ancestor].get("scene")
        if not isinstance(scene, dict):
            continue
        fields = apply_scene_location(state, scene, legacy=True)
        # Stored scenes carry resolved time/weather, including old-format saves.
        state.scene_conditions = advance_conditions(state.scene_conditions, scene)
        pinned = place_id(scene.get("location_background_id"))
        if fields["location_id"] and pinned:
            state.locations[fields["location_id"]]["background_id"] = pinned
