import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from deepseek_client import DeepSeekClient
from state_machine import StoryState
from story_events import event_prompt


def event(text, **deltas):
    return {'v': 3, 'participants': ['Анна'],
            'happening': {'subject': 'Анна', 'verb': 'узнаёт', 'object': text}, **deltas}


class EventHistoryPathTests(unittest.TestCase):
    def test_committed_events_are_not_repeated_as_future_plans(self):
        current = StoryState(genre='драма', tone='лёгкий', target_length=47)
        source = event('Позывной звучит каждые 40 секунд')
        current.record_event(source)
        current.planned_events_so_far = [event_prompt(source), '[Выбор героя] Выйти в море']
        prompt = current.to_events_prompt_block()
        self.assertEqual(prompt.count('Позывной звучит каждые 40 секунд'), 1)
        self.assertIn('[Выбор героя] Выйти в море', prompt)

    def test_delivery_and_resuming_sibling_keep_only_the_selected_raw_path(self):
        current = StoryState(genre='драма', tone='лёгкий', target_length=47)
        current.add_character('Анна', 30)
        current.add_context('Завязка: маяк')
        tree = {
            'root': {'segment_events': [event('Общая улика'), event('Предстоит выбор')],
                     'segment_choice_texts': ['А', 'Б'], 'segment_children_ids': {'А': 'a', 'Б': 'b'}},
            'a': {'segment_events': [event('Секрет только ветки А',
                      new_open_threads=['Вопрос только А'], entities=[{'name': 'ключ', 'holder': 'Анна'}])],
                  'segment_choice_texts': None, 'segment_children_ids': {}, 'origin_choice': 'А'},
            'b': {'segment_events': [event('Секрет только ветки Б')],
                  'segment_choice_texts': None, 'segment_children_ids': {}, 'origin_choice': 'Б'},
        }
        current.story_tree, current.tree_root_id = tree, 'root'
        client = DeepSeekClient(api_key='test')
        client._call = Mock(side_effect=[
            json.dumps({'scenes': [{'narration': 'PRIVATE_ROOT_1'}, {'narration': 'PRIVATE_ROOT_2'}]}),
            json.dumps({'scenes': [{'narration': 'PRIVATE_BRANCH_A'}]}),
            json.dumps({'scenes': [{'narration': 'PRIVATE_BRANCH_B'}]}),
        ])
        async def run():
            await server._ensure_materialized('test', current, 'root')
            self.assertEqual(sum('event' in r for r in current.event_history), 1)
            self.assertNotIn('Предстоит выбор', current.memory_prompt_block())
            pivot = tree['root']['children']['Продолжить']
            await server._ensure_materialized('test', current, pivot)
            self.assertEqual(sum('event' in r for r in current.event_history), 2)
            before = current.memory_prompt_block()
            await server._ensure_materialized('test', current, 'a')
            self.assertTrue(current.memory_prompt_block().startswith(before))
            self.assertIn('Секрет только ветки А', current.memory_prompt_block())
            self.assertIn('Вопрос только А', current.memory_prompt_block())
            self.assertIn('ключ', current.memory_prompt_block())
            await server._ensure_materialized('test', current, 'b')
            self.assertNotIn('Секрет только ветки А', current.memory_prompt_block())
            self.assertIn('Секрет только ветки Б', current.memory_prompt_block())
            self.assertEqual(current.player_choices, ['Б'])
            self.assertNotIn('Вопрос только А', current.memory_prompt_block())
            self.assertNotIn('"name":"ключ"', current.memory_prompt_block())
            self.assertEqual(current.current_scene, 3)
        with patch.object(server, '_client', return_value=client), patch.object(server, '_log_debug'), \
             patch.object(server, '_save_snapshot'):
            asyncio.run(run())
        self.assertEqual(client._call.call_count, 3)
        last_prompt = client._call.call_args.kwargs['messages']
        self.assertNotIn('PRIVATE_', json.dumps(last_prompt))
        self.assertNotIn('Секрет только ветки А', json.dumps(last_prompt, ensure_ascii=False))

    def test_v3_jsonl_replay_restores_source_and_never_requires_a_model(self):
        sources = [event('Сигнал каждые 40 секунд'), event('Капитан исчез 7 дней назад', ending='victory')]
        entries = [
            {'step': 'plan', 'seed': 'Маяк', 'clarifications': ['Герой — техник'], 'raw_plan': {}},
            {'step': 'characters', 'raw_characters': [{'name': 'Анна', 'age': 30}]},
            {'step': 'segment_events', 'events': sources, 'ceiling_scene': 47, 'window': None},
            {'step': 'segment_prose_final', 'events': sources, 'segment_complete': True,
             'raw_scenes': [{'narration': 'PRIVATE_PROSE_1'}, {'narration': 'PRIVATE_PROSE_2'}]},
        ]
        with patch.object(DeepSeekClient, '_call', side_effect=AssertionError('No paid replay')):
            current, _ = server._reconstruct_session(copy.deepcopy(entries))
        self.assertEqual([r['event'] for r in current.event_history if 'event' in r], sources)
        self.assertIn('Герой — техник', current.memory_prompt_block())
        self.assertNotIn('PRIVATE_', current.to_prompt_block())
        self.assertNotIn('facts', current.to_dict())
        self.assertNotIn('numeric_facts', current.to_dict())

    def test_extra_prose_is_retried_without_unplanned_scene_or_memory(self):
        current = StoryState(genre='драма', tone='лёгкий', target_length=47)
        current.add_character('Анна', 30)
        tree = {'root': {'segment_events': [event('Единственное исходное событие')],
                         'segment_choice_texts': None, 'segment_children_ids': {}}}
        current.story_tree, current.tree_root_id = tree, 'root'
        client = DeepSeekClient(api_key='test')
        client._call = Mock(side_effect=[json.dumps({'scenes': [
            {'narration': 'PRIVATE_FIRST'}, {'narration': 'PRIVATE_EXTRA'}]}),
            json.dumps({'scenes': [{'narration': 'PRIVATE_FIRST'}]})])
        async def run():
            await server._ensure_materialized('test', current, 'root')
            self.assertNotIn('Продолжить', tree['root']['children'])
        with patch.object(server, '_client', return_value=client), patch.object(server, '_log_debug'), \
             patch.object(server, '_save_snapshot'):
            asyncio.run(run())
        self.assertEqual(current.current_scene, 1)
        self.assertEqual(sum('event' in r for r in current.event_history), 1)
        self.assertNotIn('PRIVATE_', current.to_prompt_block())
        self.assertEqual(client._call.call_count, 2)
