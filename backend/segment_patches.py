"""Apply a model's segment edits locally, exactly and atomically. No API calls."""

import copy
import json
import re

from story_events import normalize_event, terminal_event


class SegmentPatchError(KeyError):
    """The review cannot be safely applied to its original draft."""


def apply_replacements(events, review, cast, *, final=False):
    if not isinstance(review, dict) or not isinstance(review.get('replacements'), list):
        raise SegmentPatchError('Проверка должна вернуть replacements — список замен')
    changes = review['replacements']
    if len(changes) > 512:
        raise SegmentPatchError('Слишком много замен сегмента')
    result = copy.deepcopy(events)
    targets = []
    prepared = []
    for change in changes:
        if not isinstance(change, dict) or not {'event_index', 'path', 'old', 'new'} <= change.keys():
            raise SegmentPatchError('Неполная замена сегмента')
        index, path = change['event_index'], change['path']
        if type(index) is not int or not 0 <= index < len(events) or not isinstance(path, str):
            raise SegmentPatchError('Неверный адрес события')
        if path and not re.fullmatch(r'[a-z_][a-z0-9_]*(?:\.(?:[a-z_][a-z0-9_]*|0|[1-9][0-9]*))*', path):
            raise SegmentPatchError('Неверный путь поля')
        parts = tuple(path.split('.')) if path else ()
        if parts and parts[0] not in {
            'participants', 'happening', 'actions', 'reactions', 'expressions', 'location',
            'conditions_change', 'atmosphere', 'screen_events', 'entities', 'resources',
            'world_rules', 'new_open_threads', 'closed_threads', 'ending', 'choices',
        }:
            raise SegmentPatchError('Поле не относится к событию')
        for other_index, other_parts in targets:
            if index == other_index and (parts[:len(other_parts)] == other_parts
                                         or other_parts[:len(parts)] == parts):
                raise SegmentPatchError('Замены перекрываются')
        targets.append((index, parts))
        parent, key = events, index
        try:
            current = events[index]
            for part in parts:
                parent = current
                key = int(part) if isinstance(parent, list) and part.isdecimal() else part
                current = parent[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise SegmentPatchError('Поле замены не существует') from exc
        # Compare JSON, including types: True must not match 1.
        if json.dumps(current, sort_keys=True, ensure_ascii=False) != json.dumps(change['old'], sort_keys=True, ensure_ascii=False):
            raise SegmentPatchError('Старое значение не совпадает с исходным событием')
        prepared.append((index, parts, copy.deepcopy(change['new'])))
    for index, parts, value in prepared:
        if not parts:
            result[index] = value
            continue
        parent = result[index]
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        parent[key] = value
    try:
        for original, updated in zip(events, result):
            if isinstance(original, dict) and original.get('v') in (2, 3):
                if not isinstance(updated, dict) or updated.get('v') != original['v']:
                    raise SegmentPatchError('Замена не должна удалять версию структурного события')
        normalized = [normalize_event(event, cast, default_version=3) for event in result]
        for source, event in zip(result, normalized):
            if isinstance(source, dict) and 'choices' in source:
                choices = source['choices']
                if not isinstance(choices, list) or any(not isinstance(c, str) or not c.strip() for c in choices):
                    raise SegmentPatchError('Неверный формат вариантов выбора')
                if choices:
                    if len(choices) != 2 or choices[0].strip().casefold() == choices[1].strip().casefold():
                        raise SegmentPatchError('Нужны два разных варианта выбора')
                    event['choices'] = copy.deepcopy(choices)
    except (KeyError, TypeError, ValueError) as exc:
        raise SegmentPatchError('Замена нарушает формат событий') from exc
    if not normalized or any(terminal_event(e) for e in normalized[:-1]):
        raise SegmentPatchError('Финал должен быть последним событием сегмента')
    if final and (not terminal_event(normalized[-1]) or any(
            isinstance(e, dict) and e.get('choices') for e in normalized)):
        raise SegmentPatchError('Финальный сегмент должен завершаться исходом без развилок')
    for event in normalized:
        if isinstance(event, dict):
            if any(cid not in cast for cid in event.get('participants', [])):
                raise SegmentPatchError('Неизвестный ID участника')
            if event.get('choices') and (event is not normalized[-1] or len(event['choices']) != 2):
                raise SegmentPatchError('Развилка должна содержать два варианта в конце сегмента')
    return normalized
