"""Ordered visual-novel text; old saved narration/dialogue remain readable."""
from character_identity import resolve_ref


def normalize_prose(scene, cast):
    result = dict(scene)
    if not isinstance(scene.get('blocks'), list):
        return result
    blocks = []
    for block in scene['blocks']:
        if not isinstance(block, dict) or block.get('type') not in {'narration', 'dialogue', 'thought'}:
            raise KeyError('неверный тип блока прозы')
        text = block.get('text')
        if not isinstance(text, str) or not text.strip():
            raise KeyError('пустой блок прозы')
        item = {'type': block['type'], 'text': text.strip()}
        if item['type'] == 'dialogue':
            cid = resolve_ref(cast, block.get('speaker_id'))
            if not cid:
                raise KeyError('неизвестный speaker_id блока')
            character = cast[cid]
            item.update(speaker_id=cid, speaker=character.get('name', '') if isinstance(character, dict) else character.name)
        blocks.append(item)
    if not blocks:
        raise KeyError('сцена не содержит блоков')
    result['blocks'] = blocks
    # Narration is an internal compatibility view, never a second UI rendering.
    # Only narrator text may ground movement; guesses/dialogue are not proof.
    result['narration'] = '\n\n'.join(b['text'] for b in blocks if b['type'] == 'narration')
    result.pop('dialogue', None)
    result.pop('speaker', None)
    result.pop('speaker_id', None)
    return result
