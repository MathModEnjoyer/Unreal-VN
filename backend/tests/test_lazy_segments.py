import asyncio
import copy
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from deepseek_client import DeepSeekClient
from prompts import (build_story_plan_prompt, build_segment_plan_directive,
                     build_character_planning_prompt, choice_windows)
from state_machine import StoryState


OUTLINE = {'segments': [
    {'places': ['улица'], 'events': ['угон машины']},
    {'places': ['квартира'], 'events': ['обыск квартиры']},
    {'places': ['площадь'], 'events': ['передача предмета']},
], 'initial_conditions': {'time_of_day': 'night', 'atmosphere': {'weather': 'clear'}}}


def event(text):
    return {'v': 3, 'participants': ['Анна'],
            'happening': {'subject': 'Анна', 'verb': 'узнаёт', 'object': text}}


def events_response(label, count=9, choices=('А', 'Б')):
    result = [event(f'{label} {i}') for i in range(count)]
    if choices:
        result[-1]['choices'] = list(choices)
    else:
        result[-1]['ending'] = 'victory'
    return json.dumps({'events': result}, ensure_ascii=False)


def prose_response(label, count=9):
    return json.dumps({'scenes': [{'narration': f'PRIVATE_PROSE_{label}_{i}'}
                                 for i in range(count)]})


def state(full=False):
    current = StoryState(genre='драма', tone='лёгкий', target_length=30,
                         build_all_branches=full, story_plan=copy.deepcopy(OUTLINE))
    current.add_character('Анна', 30)
    current.add_context('Завязка: исчезновение')
    return current


class SegmentOutlineTests(unittest.TestCase):
    def test_outline_is_requested_per_segment_and_supplied_to_cast(self):
        for length, count in ((6, 2), (30, 3), (90, 4)):
            prompt = build_story_plan_prompt('Завязка', 'драма', 'лёгкий', length, [])
            self.assertIn(f'ровно {count} последовательных сегмента', prompt)
        cast = build_character_planning_prompt('Завязка', 'драма', 'лёгкий', OUTLINE, 2, 3)
        for segment in OUTLINE['segments']:
            self.assertIn(segment['events'][0], cast)
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=json.dumps(OUTLINE))
        plan, _ = client.generate_story_plan('Завязка', 'драма', 'лёгкий', 30, [])
        self.assertEqual(plan, OUTLINE)
        client._call.assert_called_once()

    def test_outline_slot_does_not_depend_on_model_scene_count(self):
        prompt = build_segment_plan_directive(OUTLINE, 5, 15, 30, segment_index=1)
        self.assertIn('обыск квартиры', prompt)
        self.assertNotIn('угон машины', prompt)
        self.assertNotIn('передача предмета', prompt)
        legacy = build_segment_plan_directive({'premise': 'Старый замысел', 'turning_points': [
            {'phase': 'завязка', 'event': 'Старая завязка'}]}, 1, 5, 30)
        self.assertIn('Старая завязка', legacy)

    def test_local_state_is_not_repeated_and_canon_prefix_is_stable(self):
        current = state()
        current.location = 'LOCAL_ONLY_PLACE'
        current.scene_conditions = {'time_of_day': 'day'}
        current.pending_choice = 'Войти в квартиру'
        current.planned_events_so_far = ['[Выбор героя] Войти в квартиру']
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        before = client._story_messages(current, current.to_events_prompt_block(False))
        self.assertEqual(before[1]['content'].count('Войти в квартиру'), 1)
        self.assertNotIn('LOCAL_ONLY', json.dumps(before))
        self.assertIn('"time_of_day":"night"', before[0]['content'])
        current.record_event(event('Ключ остаётся у Анны'))
        after = client._story_messages(current, 'новое задание')
        self.assertEqual(before[0], after[0])
        self.assertIn('Ключ остаётся у Анны', after[1]['content'])

    def test_malformed_choice_retries_as_format_error(self):
        current = state()
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=events_response('Начало', choices=('А', 'А')))
        with self.assertRaises(KeyError):
            client.generate_segment_events(current, 1, 11, choice_windows(30)[0])

    def test_empty_choices_at_finale_do_not_trigger_another_request(self):
        current, client = state(), DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'events': [event('Финал') | {'choices': []}]}))
        events, choices, _ = client.generate_segment_events(current, 1, 30, None)
        self.assertTrue(events)
        self.assertIsNone(choices)
        client._call.assert_called_once()


class LazySegmentFlowTests(unittest.TestCase):
    def harness(self, current, responses, entries):
        stack = ExitStack()
        self.addCleanup(stack.close)
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        queue = iter(responses)
        def call(*, state=None, **kwargs):
            result = next(queue)  # any accidental recursive generation fails immediately
            state.record_usage({'prompt_tokens': 10, 'completion_tokens': 5,
                                'prompt_cache_hit_tokens': 8, 'prompt_cache_miss_tokens': 2})
            return result
        client._call = Mock(side_effect=call)
        def log(session_id, path, step, payload):
            entries.append({'step': step, **copy.deepcopy(payload)})
        stack.enter_context(patch.object(server, '_client', return_value=client))
        stack.enter_context(patch.object(server, '_log_debug', side_effect=log))
        stack.enter_context(patch.object(server, '_save_snapshot'))
        return client

    async def build(self, current, client):
        window = choice_windows(current.target_length)[0]
        tree = {}
        root = await server._build_tree_events(current, client, tree, asyncio.Semaphore(1),
                                               'test', None, window[1], window, None)
        current.story_tree, current.tree_root_id = tree, root
        return root

    def test_terminal_first_segment_does_not_prepare_alternatives(self):
        current, entries = state(True), []
        client = self.harness(current, [events_response('Финал', 1, None),
                                       prose_response('Финал', 1)], entries)
        async def run():
            root = await self.build(current, client)
            node = await server._ensure_materialized('test', current, root)
            self.assertEqual(node['scene']['ending'], 'victory')
            self.assertEqual(node['children'], {})
            self.assertEqual(client._call.call_count, 2)
        asyncio.run(run())

    def test_pending_coordinates_do_not_duplicate_canon_or_whole_history(self):
        current = state()
        current.record_event(event('История хранится в узлах пути'))
        tree = {}
        node_id = server._make_pending_branch(tree, current, 'А', 20, (16, 20))
        snapshot = tree[node_id]['pending_state']
        self.assertEqual(set(snapshot), {'planned_events_so_far', 'segment_index', 'branch_path'})
        self.assertNotIn('event_history', snapshot)
        self.assertNotIn('characters', snapshot)

    def test_identical_source_events_on_siblings_replay_their_own_prose(self):
        current, entries = state(True), []
        source = events_response('Одинаковое событие', 9, None)
        client = self.harness(current, [events_response('Начало'), prose_response('Начало'),
                                       source, source, prose_response('ТОЛЬКО_А'),
                                       prose_response('ТОЛЬКО_Б')], entries)
        async def run():
            root = await self.build(current, client)
            await server._ensure_materialized('test', current, root)
            pivot = current.story_tree[root]['segment_tail_id']
            for child in list(current.story_tree[pivot]['children'].values()):
                await server._ensure_materialized('test', current, child)
        asyncio.run(run())
        logs = [{'step': 'plan', 'raw_plan': OUTLINE, 'target_length': 30},
                {'step': 'characters', 'raw_characters': [{'name': 'Анна', 'age': 30}]}] + entries
        restored, _ = server._reconstruct_session(logs)
        tree = restored.story_tree
        pivot = tree[tree[restored.tree_root_id]['segment_tail_id']]
        for choice in ('А', 'Б'):
            narration = tree[pivot['children'][choice]]['scene']['narration']
            self.assertIn(f'ТОЛЬКО_{choice}', narration)

    def test_economy_generates_only_selected_segment_and_reuses_after_reload(self):
        current, entries = state(False), []
        client = self.harness(current, [events_response('Начало'), prose_response('Начало'),
                                       events_response('Секрет Б'), prose_response('Б')], entries)
        async def run():
            root = await self.build(current, client)
            await server._ensure_materialized('test', current, root)
            self.assertEqual(client._call.call_count, 2)
            pivot = current.story_tree[root]['segment_tail_id']
            await server._ensure_materialized('test', current, pivot)
            children = current.story_tree[pivot]['children']
            untouched, chosen = children['А'], children['Б']
            await server._ensure_materialized('test', current, chosen)
            self.assertEqual(client._call.call_count, 4)
            self.assertTrue(current.story_tree[untouched]['pending_branch'])
            self.assertNotIn('segment_events', current.story_tree[untouched])
            self.assertEqual(current.story_tree[chosen]['segment_index'], 1)
            for child in current.story_tree[current.story_tree[chosen]['segment_tail_id']]['children'].values():
                self.assertTrue(current.story_tree[child]['pending_branch'])
            prompt = client._call.call_args_list[2].kwargs['messages'][1]['content']
            self.assertIn('Начало 0', prompt)
            self.assertIn('обыск квартиры', prompt)
            self.assertIn('ПРИНЯТОЕ РЕШЕНИЕ', prompt)
            self.assertNotIn('угон машины', prompt)
            self.assertNotIn('PRIVATE_PROSE', prompt)
            self.assertEqual(current.total_api_calls, 4)
            restored = StoryState.from_dict(json.loads(json.dumps(current.to_dict())))
            await server._ensure_materialized('test', restored, chosen)
            self.assertEqual(client._call.call_count, 4)
            self.assertEqual(restored.branch_path, ['Б'])
        asyncio.run(run())

    def test_normal_mode_prepares_one_level_only_with_separate_sibling_histories(self):
        current, entries = state(True), []
        client = self.harness(current, [
            events_response('Начало'), prose_response('Начало'),
            events_response('Секрет А'), events_response('Секрет Б'),
            prose_response('А'), events_response('Финал АА', 12, None),
            events_response('Финал АБ', 12, None),
        ], entries)
        async def run():
            root = await self.build(current, client)
            self.assertEqual(client._call.call_count, 1)
            await server._ensure_materialized('test', current, root)
            self.assertEqual(client._call.call_count, 4)
            # Only first displayed scene is accepted in the live journal.
            self.assertEqual(sum('event' in r for r in current.event_history), 1)
            pivot = current.story_tree[root]['segment_tail_id']
            await server._ensure_materialized('test', current, pivot)
            children = current.story_tree[pivot]['children']
            for child in children.values():
                node = current.story_tree[child]
                self.assertIsNone(node['scene'])
                self.assertEqual(len(node['segment_events']), 9)
                for grandchild in node['segment_children_ids'].values():
                    self.assertTrue(current.story_tree[grandchild]['pending_branch'])
            await server._ensure_materialized('test', current, children['А'])
            self.assertEqual(client._call.call_count, 7)
            self.assertIsNone(current.story_tree[children['Б']]['scene'])
            prompt = client._call.call_args_list[5].kwargs['messages'][1]['content']
            self.assertIn('Секрет А', prompt)
            self.assertNotIn('Секрет Б', prompt)
            self.assertIn('передача предмета', prompt)
            self.assertNotIn('PRIVATE_PROSE', prompt)
            self.assertEqual(current.total_api_calls, 7)
            await server._ensure_materialized('test', current, root)
            self.assertEqual(client._call.call_count, 7)
        asyncio.run(run())

        # Incremental logs retain length, outline positions and repeated choice
        # labels at different depths; replay must not call the model.
        logs = [{'step': 'plan', 'raw_plan': OUTLINE, 'seed': 'Исчезновение',
                 'genre': 'драма', 'tone': 'лёгкий', 'target_length': 30},
                {'step': 'characters', 'raw_characters': [{'name': 'Анна', 'age': 30}]}] + entries
        with patch.object(server, '_client', side_effect=AssertionError('No paid replay')):
            restored, _ = server._reconstruct_session(logs)
        self.assertEqual(restored.target_length, 30)
        tree = restored.story_tree
        root = tree[restored.tree_root_id]
        pivot = tree[root['segment_tail_id']]
        branch = tree[pivot['children']['А']]
        self.assertEqual(branch['segment_index'], 1)
        a_pivot = tree[branch['segment_tail_id']]
        aa = tree[a_pivot['children']['А']]
        ab = tree[a_pivot['children']['Б']]
        self.assertIn('Финал АА', json.dumps(aa['segment_events'], ensure_ascii=False))
        self.assertIn('Финал АБ', json.dumps(ab['segment_events'], ensure_ascii=False))
        sibling = tree[pivot['children']['Б']]
        for child in sibling['segment_children_ids'].values():
            self.assertTrue(tree[child]['pending_branch'])


if __name__ == '__main__':
    unittest.main()
