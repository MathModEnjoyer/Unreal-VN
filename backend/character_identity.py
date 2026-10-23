"""Stable story-local character IDs; names are display labels, not keys.

Legacy names resolve only when unique. Unknown/ambiguous references never
select the first character, create characters, or cause another model call.
"""
import copy
import re
from collections import Counter


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'c[1-9][0-9]*', value) is not None


def next_id(used):
    index = 1
    while f'c{index}' in used:
        index += 1
    return f'c{index}'


def _value(character, key):
    return character.get(key, '') if isinstance(character, dict) else getattr(character, key, '')


def resolve_ref(cast, ref, *, aliases=False):
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    if ref in cast:
        return _value(cast[ref], 'id') or (ref if valid_id(ref) else None)
    # IDs never fall back to a similarly named character.
    if valid_id(ref):
        return None
    exact = [key for key, c in cast.items() if _value(c, 'name') == ref]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if not aliases:
        return None
    cleaned = re.sub(r'\s*\([^)]*\)', '', ref).strip().casefold()
    exact = [key for key, c in cast.items() if _value(c, 'name').casefold() == cleaned]
    if exact:
        return exact[0] if len(exact) == 1 else None
    matches = [key for key, c in cast.items() if cleaned and cleaned in _value(c, 'name').casefold().split()]
    return matches[0] if len(matches) == 1 else None


def prepare_cast(characters, relationships=()):
    """Repair missing/colliding IDs once, reserving every unique supplied ID."""
    chars = [copy.deepcopy(c) for c in characters if isinstance(c, dict) and str(c.get('name') or '').strip()]
    counts = Counter(c.get('id') for c in chars if valid_id(c.get('id')))
    reserved = {cid for cid, count in counts.items() if count == 1}
    used = set(reserved)
    for c in chars:
        cid = c.get('id')
        if not valid_id(cid) or counts[cid] != 1:
            cid = next_id(used)
            used.add(cid)
        c['id'] = cid
        c['name'] = str(c['name']).strip()
    cast = {c['id']: c for c in chars}
    ambiguous = {cid for cid, count in counts.items() if count > 1}
    rels = []
    for r in relationships if isinstance(relationships, (list, tuple)) else ():
        if not isinstance(r, dict) or not isinstance(r.get('between'), list) or len(r['between']) != 2:
            continue
        refs = [resolve_ref(cast, ref) if isinstance(ref, str) and ref not in ambiguous else None for ref in r['between']]
        if all(refs):
            rels.append({**copy.deepcopy(r), 'between': refs})
    return chars, rels


def normalize_event_refs(event, cast):
    """Convert known legacy metadata references without rewriting prose/facts."""
    if not isinstance(event, dict):
        return event
    result = copy.deepcopy(event)
    def ref(value):
        return resolve_ref(cast, value) or value
    if isinstance(result.get('participants'), list):
        result['participants'] = [ref(v) for v in result['participants']]
    if isinstance(result.get('happening'), dict):
        for key in ('subject', 'object'):
            if key in result['happening']:
                result['happening'][key] = ref(result['happening'][key])
    for key in ('actions', 'reactions'):
        if isinstance(result.get(key), list):
            for action in result[key]:
                if isinstance(action, dict):
                    for field in ('actor', 'object'):
                        if field in action:
                            action[field] = ref(action[field])
    for entity in result.get('entities', []) if isinstance(result.get('entities'), list) else []:
        if isinstance(entity, dict) and 'holder' in entity:
            entity['holder'] = ref(entity['holder'])
    return result


def dialogue_fields(cast, scene):
    """A supplied speaker_id is authoritative: never guess from its label."""
    if 'speaker_id' in scene:
        candidate = scene['speaker_id']
        cid = candidate if isinstance(candidate, str) and candidate in cast else None
    else:
        cid = resolve_ref(cast, scene.get('speaker'), aliases=True)
    label = _value(cast[cid], 'name') if cid else scene.get('speaker')
    if not isinstance(label, str) or not label.strip():
        label = 'Неизвестный' if isinstance(scene.get('dialogue'), str) and scene['dialogue'].strip() else None
    return {'speaker_id': cid, 'speaker': label}


def migrate_state(data):
    """Copy and upgrade saves, including tree scenes and deferred snapshots.

    Only typed reference fields are converted. Narrative text, dialogue,
    choices, result strings and old source history are retained verbatim.
    """
    result = copy.deepcopy(data)
    old = result.get('characters', {})
    chars = []
    for key, c in old.items():
        c = copy.deepcopy(c)
        if valid_id(key) and not c.get('id'):
            c['id'] = key
        chars.append(c)
    chars, rels = prepare_cast(chars, result.get('character_relationships', []))
    cast = {c['id']: c for c in chars}
    result['characters'] = cast
    result['character_relationships'] = rels
    for line in result.get('recent_lines', []):
        if isinstance(line, dict) and ('speaker' in line or 'speaker_id' in line):
            line.update(dialogue_fields(cast, line))
    def walk(node):
        if isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, dict):
            if 'characters' in node and 'genre' in node and 'target_length' in node:
                upgraded = migrate_state(node)
                node.clear(); node.update(upgraded)
                return
            if 'narration' in node or ('speaker' in node and 'dialogue' in node):
                node.update(dialogue_fields(cast, node))
            if isinstance(node.get('segment_events'), list):
                node['segment_events'] = [normalize_event_refs(e, cast) for e in node['segment_events']]
            # Historical source records are append-only. Legacy name references
            # remain understandable via the fixed ID/name cast table.
            for key, value in list(node.items()):
                if key not in {'event_history', 'history_records', 'segment_events'}:
                    walk(value)
    walk(result.get('story_tree', {}))
    return result
