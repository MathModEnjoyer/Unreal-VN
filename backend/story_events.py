"""Compact event contracts; old text-only plans remain readable."""

import copy
import json

from scene_conditions import advance_conditions
from character_sprites import expression_changes
from character_identity import normalize_event_refs
from background_effects import scene_effect_context


def event_text(event):
    if isinstance(event, str):
        return event
    if not isinstance(event, dict):
        return ""
    parts = []
    happening = event.get("happening", {})
    if isinstance(happening, dict):
        parts.append(" ".join(str(happening.get(k) or "") for k in ("subject", "verb", "object")).strip())
    for key in ("actions", "reactions"):
        for action in event.get(key, []):
            parts.append(" ".join(str(action.get(k) or "") for k in ("actor", "verb", "object")).strip())
    parts.extend(event.get("results", []))
    return "; ".join(filter(None, parts)) or str(event.get("event") or "")


def event_prompt(event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if isinstance(event, dict) else str(event)


def _text(value, limit=180):
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalize_event(raw, cast, *, default_version=1):
    """Normalize JSON shapes without judging participants or story consistency."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()  # saved/custom plans from the older format
    if not isinstance(raw, dict):
        raise KeyError("неверный формат события")
    if "happening" not in raw and isinstance(raw.get("event"), str) and raw["event"].strip():
        return raw["event"].strip()
    raw = normalize_event_refs(raw, cast)
    happening = raw.get("happening")
    participants = raw.get("participants")
    if not isinstance(happening, dict) or not _text(happening.get("verb")):
        raise KeyError("событие не содержит действия")
    participants = [n for n in participants if isinstance(n, str)] if isinstance(participants, list) else []
    version = raw.get("v", default_version)
    modern = version in (2, 3)
    result = {
        "participants": list(dict.fromkeys(participants)),
        "happening": {k: _text(happening.get(k), None) for k in ("subject", "verb", "object")},
    }
    expressions = {cid: value for cid, value in expression_changes(raw.get("expressions"), cast).items()
                   if cid in participants}
    if expressions:
        result["expressions"] = expressions
    for key in ("actions", "reactions"):
        actions = raw.get(key, [])
        if not isinstance(actions, list):
            raise KeyError("неверный список действий события")
        result[key] = []
        for action in actions:
            if not isinstance(action, dict) or not _text(action.get("verb")):
                raise KeyError("неверный формат действия события")
            result[key].append({k: _text(action.get(k), None) for k in ("actor", "verb", "object")})
    if version != 3:  # Read old source fields without extracting separate memory.
        results = raw.get("results", [])
        if not isinstance(results, list) or any(not isinstance(fact, str) for fact in results):
            raise KeyError("неверные результаты события")
        result["results"] = [_text(fact, None) for fact in results if _text(fact, None)]
        numbers = raw.get("numbers", [])
        if not isinstance(numbers, list):
            raise KeyError("неверный числовой реестр события")
        result["numbers"] = []
        for number in numbers:
            if not isinstance(number, dict) or not _text(number.get("key")) or not _text(number.get("value")):
                raise KeyError("неверный числовой факт")
            result["numbers"].append({"key": _text(number["key"], None), "value": _text(number["value"], None)})
    location = raw.get("location")
    if isinstance(location, dict) and _text(location.get("id")):
        result["location"] = {"id": _text(location["id"], 80), "label": _text(location.get("label"), 100)}
    if 'location' in result and isinstance(location.get('background_id'), str):
        result['location']['background_id'] = location['background_id']
    if isinstance(raw.get("conditions_change"), dict):
        result["conditions_change"] = advance_conditions({}, {"conditions_change": raw["conditions_change"]})
    local = scene_effect_context(raw)
    if local.get("atmosphere"):
        result["atmosphere"] = {k: v for k, v in local["atmosphere"].items()
                                if k in {"ambient", "ambient_source", "ambient_intensity"}}
        if not result["atmosphere"]:
            result.pop("atmosphere")
    if isinstance(raw.get("screen_events"), list):
        result["screen_events"] = [
            {"type": "flash", "source": item["source"], "intensity": item.get("intensity", 0.3)}
            for item in raw["screen_events"][:3]
            if isinstance(item, dict) and item.get("type") == "flash"
            and item.get("source") in {"lightning", "explosion", "camera", "magic", "impact"}
            and isinstance(item.get("intensity", 0.3), (int, float))
        ]
    if modern:
        result["v"] = version
        # Reactions are actions too; preserve their order after legacy actions.
        actions = result.get("actions", []) + result.pop("reactions", [])
        main = result["happening"]
        if actions and all(actions[0].get(a) == main.get(b)
                           for a, b in (("actor", "subject"), ("verb", "verb"), ("object", "object"))):
            actions = actions[1:]
        result["actions"] = actions
        if "location" in result and location.get("transition") in ("move", "cut"):
            result["location"]["transition"] = location["transition"]
        for key in ("world_rules", "new_open_threads", "closed_threads"):
            values = raw.get(key, [])
            if isinstance(values, list):
                result[key] = [v.strip() for v in values if isinstance(v, str) and v.strip()]
        for key, fields in (("entities", ("name", "kind", "holder", "location", "lost", "traits")),
                            ("resources", ("name", "amount"))):
            values = raw.get(key, [])
            if isinstance(values, list):
                result[key] = [{k: item[k] for k in fields if k in item}
                               for item in values if isinstance(item, dict) and _text(item.get("name"))]
        if raw.get("ending") in ("death", "failure", "victory"):
            result["ending"] = raw["ending"]
        # Sparse deltas reduce both the response and subsequent path context.
        result = {k: v for k, v in result.items()
                  if k == "participants" or v not in (None, [], {})}
    return result


def bind_event(scene, event):
    """Bind planned memory and visual metadata to generated text."""
    result = dict(scene)
    if not isinstance(event, dict):
        if isinstance(event, str):
            result["_planned_event"] = event
        return result
    if event.get("v") in (2, 3):
        # Only text comes from prose. All state
        # deltas have one owner, including absent fields (which mean no change).
        result = {key: scene[key] for key in ("narration", "speaker_id", "speaker", "dialogue", "blocks")
                  if key in scene}
        for key in ("entities", "resources", "world_rules", "new_open_threads", "closed_threads", "ending"):
            if key in event:
                result[key] = event[key]
        location = event.get("location")
        if isinstance(location, dict):
            result["location_id"] = location.get("id")
            result["location"] = location.get("label")
            if "background_id" in location:
                result["location_background_id"] = location["background_id"]
            if location.get("transition") in ("move", "cut"):
                result["location_transition"] = {"kind": location["transition"]}
    result["_planned_event"] = event
    # Planning owns the choice of time/weather/effects. Prose supplies the
    # text; scene_locations validates the event's destination and transition.
    for key in ("conditions_change", "atmosphere", "screen_events"):
        result.pop(key, None)
        if key in event:
            result[key] = event[key]
    result.pop("time_of_day", None)
    return result


def terminal_event(event):
    return (isinstance(event, dict) and event.get("v") in (2, 3)
            and event.get("ending") in ("death", "failure", "victory"))


def through_ending(events):
    """No further planning after a declared ending; legacy plans are unchanged."""
    for i, event in enumerate(events):
        if terminal_event(event):
            return events[:i + 1]
    return events


def restore_event_path(state, tree, node_id, *, include_current=False):
    """Restore raw history from this path, including cached prose/JSONL branches.

    Nodes keep only their own history delta. Missing deltas in an old snapshot
    retain its imported legacy context instead of erasing information.
    """
    root = state.tree_root_id
    if root not in tree or node_id not in tree:
        return
    parents, pending = {root: None}, [root]
    while pending and node_id not in parents:
        current = pending.pop()
        children = tree[current].get("children") or tree[current].get("segment_children_ids") or {}
        for child in children.values():
            if child in tree and child not in parents:
                parents[child] = current
                pending.append(child)
    if node_id not in parents:
        return
    path, current = [], node_id if include_current else parents[node_id]
    while current is not None:
        path.append(current)
        current = parents[current]
    path.reverse()
    if any(tree[n].get("scene") is not None and "history_records" not in tree[n] for n in path):
        return
    records = [copy.deepcopy(r) for r in state.event_history if "context" in r]
    for n in path:
        records.extend(copy.deepcopy(tree[n].get("history_records", [])))
    state.event_history = records
    state.player_choices = [r["choice"] for r in records if "choice" in r]
    state.character_expressions = {}
    for record in records:
        context = record.get("context")
        if isinstance(context, dict) and isinstance(context.get("legacy_state"), dict):
            legacy = context["legacy_state"]
            state.player_choices[:0] = legacy.get("player_choices", [])
        source = record.get("event", {})
        if isinstance(source, dict):
            state.character_expressions.update(expression_changes(source.get("expressions"), state.characters))
    scenes = [r["scene"] for r in records if "event" in r]
    # Accepted extra prose can have no planned event; it still counts as a scene.
    state.current_scene = max(max(scenes, default=0),
                              sum(isinstance(tree[n].get("scene"), dict) for n in path))
