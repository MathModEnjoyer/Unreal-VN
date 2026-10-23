"""Offline integration tests: draft -> segment review -> local patches -> prose."""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from character_sprites import assign_sprites, scene_sprites
from deepseek_client import DeepSeekClient
from segment_patches import apply_replacements, SegmentPatchError
from state_machine import StoryState
from story_events import normalize_event, bind_event


def state():
    s = StoryState(genre='фантастика', tone='загадочный', target_length=12, build_all_branches=False)
    s.add_character('Алекс', 34, 'штурман', character_id='c1', is_protagonist=True)
    s.add_context('Завязка: герой проснулся на корабле.')
    s.add_context('Уточнение: играем за штурмана, а не инженера.')
    return s


def event(text='ключ остался в разъёме', **extra):
    return {'v': 3, 'participants': ['c1'],
            'happening': {'subject': 'c1', 'verb': 'обнаруживает', 'object': text}, **extra}


def replacement(index, path, old, new):
    return {'event_index': index, 'path': path, 'old': old, 'new': new}


class LocalPatchTests(unittest.TestCase):
    def test_multiple_exact_edits_and_whole_event_replacement_are_atomic(self):
        s = state()
        source = [event(), event('победа без действия')]
        original = copy.deepcopy(source)
        final = event('отключает питание запертого отсека и останавливает угрозу', ending='victory')
        review = {'replacements': [replacement(0, 'happening.object', 'ключ остался в разъёме',
                                               'извлекает ключ из разъёма'),
                                   replacement(1, '', source[1], final)]}
        result = apply_replacements(source, review, s.characters, final=True)
        self.assertEqual(source, original)
        self.assertEqual(result[0]['happening']['object'], 'извлекает ключ из разъёма')
        self.assertEqual(result[1]['ending'], 'victory')
        self.assertEqual(len(result), len(source))

    def test_stale_old_value_never_partially_changes_input(self):
        source = [event(), event('воздуха мало')]
        before = copy.deepcopy(source)
        changes = [replacement(0, 'happening.object', source[0]['happening']['object'], 'новый текст'),
                   replacement(1, 'happening.object', 'неверный текст', 'другой текст')]
        with self.assertRaises(SegmentPatchError):
            apply_replacements(source, {'replacements': changes}, state().characters)
        self.assertEqual(source, before)

    def test_rejects_overlapping_missing_unsafe_or_boolean_addresses(self):
        source = [event(actions=[{'actor': 'c1', 'verb': 'берёт', 'object': 'рычаг'}])]
        invalid = [
            [replacement(True, '', source[0], source[0])],
            [replacement(0, 'actions.-1.object', 'рычаг', 'ключ')],
            [replacement(0, '__class__', None, 'x')],
            [replacement(2, '', {}, {})],
            [replacement(0, 'ending', None, 'victory')],
            [replacement(0, 'happening', source[0]['happening'], source[0]['happening']),
             replacement(0, 'happening.object', source[0]['happening']['object'], 'x')],
            [replacement(0, '', source[0], source[0]), replacement(0, 'participants', ['c1'], [])],
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(SegmentPatchError):
                apply_replacements(source, {'replacements': changes}, state().characters)

    def test_array_indices_and_character_expression_keys(self):
        source = [event(actions=[{'actor': 'c1', 'verb': 'берёт', 'object': 'рычаг'}],
                        expressions={'c1': 'smile'})]
        result = apply_replacements(source, {'replacements': [
            replacement(0, 'actions.0.object', 'рычаг', 'трос'),
            replacement(0, 'expressions.c1', 'smile', 'tension')]}, state().characters)
        self.assertEqual(result[0]['actions'][0]['object'], 'трос')
        self.assertEqual(result[0]['expressions']['c1'], 'tension')

    def test_final_requires_terminal_outcome_and_no_future_choice(self):
        for source in ([event()], [event(ending='victory', choices=['А', 'Б'])],
                       [event(ending='victory'), event('ещё не конец')]):
            with self.subTest(source=source), self.assertRaises(SegmentPatchError):
                apply_replacements(source, {'replacements': []}, state().characters, final=True)
        result = apply_replacements([event(ending='failure')], {'replacements': []},
                                    state().characters, final=True)
        self.assertEqual(result[-1]['ending'], 'failure')

    def test_no_replacements_and_bad_review_schema(self):
        s = state()
        source = [normalize_event(event(), s.characters)]
        self.assertEqual(apply_replacements(source, {'replacements': []}, s.characters), source)
        for review in ({}, {'replacements': 'none'}, {'replacements': [{}]}):
            with self.subTest(review=review), self.assertRaises(SegmentPatchError):
                apply_replacements(source, review, s.characters)

    def test_whole_event_replacement_cannot_downgrade_structure_to_legacy(self):
        source = [event()]
        for new in ('просто текст', {'event': 'просто текст'}, {**event(), 'v': 1}):
            with self.subTest(new=new), self.assertRaises(SegmentPatchError):
                apply_replacements(source, {'replacements': [replacement(0, '', source[0], new)]},
                                   state().characters)


class SegmentReviewFlowTests(unittest.TestCase):
    def client(self, responses):
        client = DeepSeekClient(api_key='offline-test')
        client._call = Mock(side_effect=[json.dumps(r, ensure_ascii=False) for r in responses])
        return client

    def setup_tree(self, s, events, *, window=None, choices=None):
        node = {'scene': None, 'materialized': False, 'segment_events': events,
                'segment_choice_texts': choices, 'segment_children_ids': {},
                'segment_review_status': 'pending', 'segment_window': window,
                'segment_ceiling': 12, 'segment_index': 0, 'branch_path': [], 'origin_choice': None}
        s.story_tree = {'root': node}
        s.tree_root_id = 'root'

    def test_full_generation_review_prose_replay_and_revisit_without_api(self):
        s = state()
        draft = event('угроза ещё в трюме', ending='victory')
        normalized = normalize_event(draft, s.characters)
        corrected = copy.deepcopy(normalized)
        corrected['happening'] = {'subject': 'c1', 'verb': 'запирает', 'object': 'угрозу в герметичном отсеке'}
        client = self.client([{'events': [draft]}, {'replacements': [replacement(0, '', normalized, corrected)]},
                              {'scenes': [{'blocks': [{'type': 'narration', 'text': 'Я запер отсек. Угроза устранена.'}]}]}])
        entries = [{'step': 'plan', 'raw_plan': {}, 'target_length': 12},
                   {'step': 'characters', 'raw_characters': [vars(s.characters['c1'])]}]
        def log(sid, path, step, payload):
            entries.append({'step': step, **copy.deepcopy(payload)})
        async def run():
            tree = s.story_tree
            root = await server._build_tree_events(s, client, tree, asyncio.Semaphore(1),
                                                   'offline', None, 12, None, None)
            s.tree_root_id = root
            await server._materialize_segment('offline', None, s, root, tree, client)
            self.assertEqual(tree[root]['scene']['ending'], 'victory')
            self.assertIn('герметичном', s.memory_prompt_block())
            self.assertNotIn('угроза ещё в трюме', s.memory_prompt_block())
        with patch.object(server, '_log_debug', side_effect=log), patch.object(server, '_save_snapshot'):
            asyncio.run(run())
        self.assertEqual([c.kwargs['operation'] for c in client._call.call_args_list],
                         ['generate_segment_events', 'review_segment_events', 'generate_segment_prose'])
        review_prompt = client._call.call_args_list[1].kwargs['messages'][1]['content']
        self.assertIn('штурмана', review_prompt)
        self.assertIn('ФИНАЛЬНЫЙ', review_prompt)
        prose_prompt = client._call.call_args_list[2].kwargs['messages'][1]['content']
        self.assertIn('герметичном', prose_prompt)
        self.assertNotIn('угроза ещё в трюме', prose_prompt)
        with patch.object(DeepSeekClient, '_call', side_effect=AssertionError('Paid replay')):
            restored, _ = server._reconstruct_session(entries)
            self.assertEqual(restored.story_tree[restored.tree_root_id]['scene']['ending'], 'victory')
        snapshot = StoryState.from_dict(json.loads(json.dumps(s.to_dict())))
        with patch.object(server, '_client', side_effect=AssertionError('Paid revisit')):
            asyncio.run(server._ensure_materialized('offline', snapshot, snapshot.tree_root_id))

    def test_failed_review_keeps_draft_and_prose_unrequested(self):
        s = state()
        draft = normalize_event(event(ending='victory'), s.characters)
        self.setup_tree(s, [draft])
        client = self.client([{'replacements': [replacement(0, 'happening.object', 'wrong', 'new')]}] * 2)
        with patch.object(server, '_save_snapshot') as save, patch.object(server, '_log_debug'), \
             self.assertRaises(server.HTTPException):
            asyncio.run(server._materialize_segment('offline', None, s, 'root', s.story_tree, client))
        self.assertEqual(s.story_tree['root']['segment_events'], [draft])
        self.assertEqual(s.story_tree['root']['segment_review_status'], 'pending')
        self.assertEqual([c.kwargs['operation'] for c in client._call.call_args_list], ['review_segment_events'] * 2)
        save.assert_called_once()
        self.assertEqual(sum('event' in r for r in s.event_history), 0)

    def test_review_is_not_paid_again_after_prose_failure_or_restart(self):
        s = state()
        draft = normalize_event(event(ending='victory'), s.characters)
        self.setup_tree(s, [draft])
        client = self.client([{'replacements': []}, {}, {}, {}])
        with patch.object(server, '_save_snapshot'), patch.object(server, '_log_debug'), \
             self.assertRaises(server.HTTPException):
            asyncio.run(server._materialize_segment('offline', None, s, 'root', s.story_tree, client))
        self.assertEqual(s.story_tree['root']['segment_review_status'], 'complete')
        s = StoryState.from_dict(json.loads(json.dumps(s.to_dict())))
        client = self.client([{'scenes': [{'narration': 'История закончилась.'}]}])
        with patch.object(server, '_save_snapshot'), patch.object(server, '_log_debug'):
            asyncio.run(server._materialize_segment('offline', None, s, 'root', s.story_tree, client))
        self.assertEqual(client._call.call_args.kwargs['operation'], 'generate_segment_prose')
        client._call.assert_called_once()

    def test_reviewed_choices_rebuild_only_deferred_children(self):
        s = state()
        s.current_scene = 5
        source = normalize_event(event('требуется решение'), s.characters)
        self.setup_tree(s, [source], window=[4, 8], choices=['А', 'Б'])
        reviewed_source = {**copy.deepcopy(source), 'choices': ['А', 'Б']}
        client = self.client([{'replacements': [replacement(0, 'choices', ['А', 'Б'], ['В', 'Г'])]}])
        with patch.object(server, '_save_snapshot'), patch.object(server, '_log_debug'):
            asyncio.run(server._review_pending_segment('offline', None, s, 'root', s.story_tree, client))
        self.assertEqual(s.story_tree['root']['segment_choice_texts'], ['В', 'Г'])
        self.assertEqual(set(s.story_tree['root']['segment_children_ids']), {'В', 'Г'})
        self.assertEqual(reviewed_source['choices'], ['А', 'Б'])
        for child in s.story_tree['root']['segment_children_ids'].values():
            self.assertTrue(s.story_tree[child]['pending_branch'])

    def test_jsonl_draft_remains_pending_but_reviewed_draft_is_reused(self):
        s = state()
        source = normalize_event(event(ending='victory'), s.characters)
        logs = [{'step': 'plan', 'raw_plan': {}, 'target_length': 12},
                {'step': 'characters', 'raw_characters': [vars(s.characters['c1'])]},
                {'step': 'segment_events', 'events': [source], 'segment_index': 0, 'branch_path': [],
                 'window': None, 'ceiling_scene': 12, 'review_required': True}]
        restored, _ = server._reconstruct_session(logs)
        self.assertEqual(restored.story_tree[restored.tree_root_id]['segment_review_status'], 'pending')
        logs.append({'step': 'segment_review', 'events': [source], 'segment_index': 0, 'branch_path': []})
        restored, _ = server._reconstruct_session(logs)
        self.assertEqual(restored.story_tree[restored.tree_root_id]['segment_review_status'], 'complete')


class CastingAndLocationTests(unittest.TestCase):
    def test_cast_receives_every_initial_answer_and_nonhuman_survives_save(self):
        s = state()
        s.add_context('Уточнение: Гермес — бесплотный ИИ.')
        client = DeepSeekClient(api_key='offline')
        client._call = Mock(return_value=json.dumps({'characters': [
            {'id': 'c2', 'name': 'Гермес', 'kind': 'nonhuman', 'age': 40,
             'appearance': {'gender': 'man'}, 'traits': []}]}))
        chars, _, _ = client.generate_characters('затравка', s.genre, s.tone, {}, 2, 3, s)
        prompt = client._call.call_args.kwargs['messages'][0]['content']
        self.assertIn('играем за штурмана', prompt)
        self.assertIn('бесплотный ИИ', prompt)
        self.assertEqual(chars[0]['kind'], 'nonhuman')
        s.add_character('Гермес', 40, kind=chars[0]['kind'], appearance_features={'gender': 'man'}, character_id='c2')
        assign_sprites(s)
        self.assertEqual(s.characters['c2'].sprite_id, '')
        restored = StoryState.from_dict(json.loads(json.dumps(s.to_dict())))
        sprite = scene_sprites(restored, {'participants': ['c2']})[0]['sprite']
        self.assertTrue(sprite['is_fallback'])
        self.assertEqual(sprite['reason'], 'nonhuman_character')
        self.assertEqual(restored.characters['c2'].kind, 'nonhuman')

    def test_event_transition_moves_without_quote_and_prose_cannot_invent_one(self):
        s = state()
        office = normalize_event(event(location={'id': 'office', 'label': 'Кабинет'}), s.characters)
        server.DeepSeekClient.apply_scene_to_state(s, bind_event({'narration': 'Кабинет.'}, office))
        harbor = normalize_event(event(location={'id': 'harbor', 'label': 'Порт', 'transition': 'move'}), s.characters)
        fields = server.DeepSeekClient.apply_scene_to_state(s, bind_event({'narration': 'Порт.'}, harbor))
        self.assertEqual(fields['location_id'], 'harbor')
        self.assertEqual(fields['location_transition'], {'kind': 'move'})
        fabricated = bind_event({'narration': 'Я перешёл в кабинет.', 'location_transition': {'kind': 'move'}}, office)
        fields = server.DeepSeekClient.apply_scene_to_state(s, fabricated)
        self.assertEqual(fields['location_id'], 'harbor')
        self.assertEqual(fields['location_issue'], 'location_transition_required')


if __name__ == '__main__':
    unittest.main()
