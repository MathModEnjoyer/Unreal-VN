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
from prompts import build_segment_events_prompt, build_segment_prose_prompt, build_custom_choice_events_prompt
from state_machine import StoryState
from story_events import bind_event, normalize_event


def state():
    current = StoryState(genre='драма', tone='лёгкий', target_length=12)
    current.add_character('Анна', 30, is_protagonist=True)
    return current


def event(**fields):
    return {'v': 2, 'participants': ['Анна'],
            'happening': {'subject': 'Анна', 'verb': 'находит', 'object': 'письмо'},
            **fields}


def normalized(**fields):
    return normalize_event(event(**fields), state().characters)


class EventSchemaV2Tests(unittest.TestCase):
    def test_actions_include_reactions_without_repeating_main_happening(self):
        result = normalized(actions=[{'actor': 'Анна', 'verb': 'находит', 'object': 'письмо'},
                                     {'actor': 'Анна', 'verb': 'вскрывает', 'object': 'конверт'}],
                            reactions=[{'actor': 'Анна', 'verb': 'прячет', 'object': 'письмо'}])
        self.assertEqual([a['verb'] for a in result['actions']], ['вскрывает', 'прячет'])
        self.assertNotIn('reactions', result)
        for empty in ('results', 'numbers', 'entities', 'resources', 'world_rules', 'closed_threads'):
            self.assertNotIn(empty, result)
        self.assertEqual(normalized(participants=[])['participants'], [])

    def test_plan_owns_all_state_and_prose_cannot_override_or_add_it(self):
        current = state()
        planned = normalized(
            results=['Подпись капитана подтверждена'],
            numbers=[{'key': 'срок', 'value': '7 дней'}],
            entities=[{'name': 'письмо', 'holder': 'Анна', 'traits': ['красная печать'], 'shown_change': True}],
            resources=[{'name': 'топливо', 'amount': 'половина', 'shown_refill': True}],
            world_rules=['Сюда не проходит сигнал'], new_open_threads=['Кто отправитель?'],
            closed_threads=['Где капитан?'],
            location={'id': 'office', 'label': 'Кабинет'},
            conditions_change={'time_of_day': 'night'}, ending='victory')
        self.assertNotIn('shown_change', planned['entities'][0])
        self.assertNotIn('shown_refill', planned['resources'][0])
        scene = {'narration': 'Анна сверила подпись.', 'new_facts': ['Выдумка прозы'],
                 'new_character': {'name': 'Лишний', 'age': 12},
                 'entities': [{'name': 'письмо', 'holder': 'Лишний'}],
                 'resources': [{'name': 'топливо', 'amount': 'полный'}],
                 'world_rules': ['Чужое правило'], 'new_open_threads': ['Чужая нить'],
                 'closed_threads': ['Кто отправитель?'],
                 'conditions_change': {'time_of_day': 'day'}, 'ending': 'failure',
                 'location_id': 'wrong', 'location': 'Другое место', 'choices': ['Лишний выбор']}
        bound = bind_event(scene, planned)
        self.assertNotIn('new_character', bound)
        self.assertNotIn('new_facts', bound)
        self.assertNotIn('choices', bound)
        # Direct application also enforces the binding contract.
        fields = DeepSeekClient.apply_scene_to_state(current, {**scene, '_planned_event': planned})
        self.assertEqual(fields['location_id'], 'office')
        self.assertEqual(current.event_history[-1]['event']['entities'][0]['holder'], 'c1')
        self.assertEqual(current.event_history[-1]['event']['resources'][0]['amount'], 'половина')
        self.assertEqual(current.event_history[-1]['event'], planned)
        self.assertIn('7 дней', current.memory_prompt_block())
        self.assertEqual(current.event_history[-1]['event']['new_open_threads'], ['Кто отправитель?'])
        self.assertEqual(current.event_history[-1]['event']['world_rules'], ['Сюда не проходит сигнал'])
        self.assertNotIn('Лишний', current.characters)
        self.assertEqual(current.scene_conditions['time_of_day'], 'night')
        self.assertEqual(bound['ending'], 'victory')

    def test_absent_plan_deltas_keep_source_history_and_drop_unsolicited_prose(self):
        current = state()
        first = normalized(resources=[{'name': 'топливо', 'amount': 'половина'}])
        DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'Запас проверен.'}, first))
        previous = copy.deepcopy(current.event_history)
        raw = {'narration': 'Анна ждёт.', 'resources': [{'name': 'топливо', 'amount': 'полный'}],
               'entities': [{'name': 'новый предмет'}], 'new_open_threads': ['Лишняя нить'],
               'atmosphere': {'ambient': 'ash'}, 'ending': 'death'}
        bound = bind_event(raw, normalized())
        DeepSeekClient.apply_scene_to_state(current, bound)
        self.assertEqual(current.event_history[:-1], previous)
        for field in ('resources', 'entities', 'new_open_threads', 'ending', 'atmosphere'):
            self.assertNotIn(field, bound)
        self.assertNotIn('полный', current.memory_prompt_block())

    def test_sparse_entity_updates_preserve_both_source_events_without_proof_flags(self):
        current = state()
        first = normalized(entities=[{'name': 'ключ', 'kind': 'предмет', 'holder': 'Анна', 'traits': ['медный']}])
        second = normalized(entities=[{'name': 'ключ', 'holder': 'ящик'}],
                            resources=[{'name': 'вода', 'amount': 'половина'}])
        for planned in (first, second):
            DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'Ключ перемещён.'}, planned))
        self.assertEqual([r['event'] for r in current.event_history], [first, second])
        for value in ('медный', 'ящик', 'половина'):
            self.assertIn(value, current.memory_prompt_block())

    def test_unplanned_prose_after_terminal_event_does_not_advance_or_add_memory(self):
        current = state()
        tree = {'root': {'segment_events': [normalized(ending='victory')],
                         'segment_choice_texts': None, 'segment_children_ids': {}, 'origin_choice': None}}
        current.story_tree = tree
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'scenes': [
            {'narration': 'История завершена.'},
            {'narration': 'Неприменимое продолжение.', 'new_facts': ['После финала']}]}))
        with patch.object(server, '_log_debug'):
            asyncio.run(server._materialize_segment('test', None, current, 'root', tree, client))
        self.assertEqual(current.current_scene, 1)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree['root']['scene']['ending'], 'victory')
        self.assertNotIn('После финала', current.memory_prompt_block())

    def test_plan_owns_transition_without_quote_from_prose(self):
        current = state()
        DeepSeekClient.apply_scene_to_state(current, bind_event(
            {'narration': 'Анна сидит за столом.'},
            normalized(location={'id': 'office', 'label': 'Кабинет'})))
        move = normalized(location={'id': 'harbor', 'label': 'Порт, причал', 'background_id': 'harbor_quay', 'transition': 'move'})
        unproven = bind_event({'narration': 'Анна думает о причале.',
                              'dialogue': 'Мы вышли на портовый причал.',
                              'transition_evidence': 'Мы вышли на портовый причал.'}, move)
        self.assertEqual(DeepSeekClient.apply_scene_to_state(current, unproven)['location_id'], 'harbor')
        valid = bind_event({'narration': 'Мы вышли на портовый причал. Дул ветер.',
                            'transition_evidence': 'Мы вышли на портовый причал.'}, move)
        moved = DeepSeekClient.apply_scene_to_state(current, valid)
        self.assertEqual(moved['location_id'], 'harbor')
        self.assertEqual(moved['location_background_id'], 'harbor_quay')
        back = normalized(location={'id': 'office', 'label': 'Старый кабинет', 'transition': 'cut'})
        returned = DeepSeekClient.apply_scene_to_state(current, bind_event(
            {'narration': 'Позже мы снова сидели в кабинете.',
             'transition_evidence': 'Позже мы снова сидели в кабинете.'}, back))
        self.assertEqual(returned['location_id'], 'office')
        self.assertEqual(returned['location'], 'Кабинет')

    def test_new_prose_schema_does_not_request_repeated_metadata(self):
        prompt = build_segment_prose_prompt([normalized()], ('Ждать', 'Уйти'))
        for field in ('new_facts', 'new_character', 'new_open_threads', 'closed_threads',
                      'world_rules', 'shown_change', 'shown_refill', 'location_id',
                      'location_transition', 'conditions_change', 'screen_events', '"choices"'):
            self.assertNotIn(field, prompt, field)
        self.assertIn('transition_evidence', prompt)
        for prompt in (build_segment_events_prompt('state', '', 1, 4, None),
                       build_custom_choice_events_prompt('state', 'Ждать', 4)):
            for field in ('entities', 'resources', 'world_rules', 'new_open_threads', 'closed_threads'):
                self.assertIn(field, prompt)
            self.assertNotIn('"reactions"', prompt)
            self.assertNotIn('shown_change', prompt)
            self.assertNotIn('shown_refill', prompt)

    def test_legacy_and_mixed_prompts_keep_needed_metadata_and_existing_scenes(self):
        old = normalize_event({'happening': {'verb': 'ждёт'},
                               'actions': [], 'reactions': [], 'results': []}, {})
        self.assertNotIn('v', old)
        for events in (['Старое событие'], [old], [normalized(), old]):
            prompt = build_segment_prose_prompt(events, None)
            self.assertIn('entities', prompt)
            self.assertIn('resources', prompt)
            self.assertIn('location_transition', prompt)
        current = state()
        legacy = bind_event({'narration': 'Анна получила ключ.',
                             'entities': [{'name': 'ключ', 'holder': 'Анна'}],
                             'resources': [{'name': 'вода', 'amount': 'полный'}]}, old)
        DeepSeekClient.apply_scene_to_state(current, legacy)
        self.assertEqual(current.event_history[-1]['legacy_changes']['entities'][0]['holder'], 'Анна')
        self.assertEqual(current.event_history[-1]['legacy_changes']['resources'][0]['amount'], 'полный')

    def test_generated_events_default_to_v3_without_extra_requests_or_state_commit(self):
        current = state()
        raw = event(resources=[{'name': 'вода', 'amount': 'полный'}])
        raw.pop('v')
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'events': [raw]}))
        events, _, _ = client.generate_segment_events(current, 1, 4, None)
        client._call.assert_called_once()
        self.assertEqual(events[0]['v'], 3)
        self.assertEqual(events[0]['resources'][0]['name'], 'вода')
        self.assertEqual(current.event_history, [])

    def test_early_planned_ending_stops_tree_and_never_builds_children(self):
        current = state()
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'events': [
            event(ending='failure', results=['Миссия провалена']),
            event(results=['Невозможное продолжение'], choices=['А', 'Б'])]}))
        tree = {}
        with patch.object(server, '_log_debug'):
            root = asyncio.run(server._build_tree_events(current, client, tree, asyncio.Semaphore(1),
                                                         'test', None, 4, (2, 4), None))
        client._call.assert_called_once()
        self.assertEqual(len(tree), 1)
        self.assertEqual(len(tree[root]['segment_events']), 1)
        self.assertFalse(tree[root]['segment_children_ids'])
        self.assertIsNone(tree[root]['segment_choice_texts'])
        self.assertEqual(current.event_history, [])

    def test_materialization_and_jsonl_replay_preserve_planned_deltas_places_and_pin(self):
        current = state()
        events = [normalized(location={'id': 'office', 'label': 'Кабинет'},
                             entities=[{'name': 'письмо', 'holder': 'Анна'}],
                             new_open_threads=['Кто отправитель?']),
                  normalized(location={'id': 'harbor', 'label': 'Порт, причал', 'background_id': 'harbor_quay', 'transition': 'move'},
                             resources=[{'name': 'вода', 'amount': 'половина'}],
                             closed_threads=['Кто отправитель?'], results=['Отправитель найден'],
                             conditions_change={'time_of_day': 'night', 'atmosphere': {'weather': 'rain'}},
                             ending='victory')]
        raw = [{'narration': 'Анна сидела в кабинете.'},
               {'narration': 'Мы вышли на портовый причал.',
                'transition_evidence': 'Мы вышли на портовый причал.'}]
        tree = {'root': {'segment_events': events, 'segment_choice_texts': None,
                         'segment_children_ids': {}, 'origin_choice': None}}
        current.story_tree = tree
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'scenes': raw}))
        entries = [{'step': 'plan', 'genre': 'драма', 'tone': 'лёгкий', 'raw_plan': {}},
                   {'step': 'characters', 'raw_characters': [vars(current.characters['c1'])]},
                   {'step': 'segment_events', 'events': events, 'ceiling_scene': 2, 'window': None}]
        def log(session, path, step, payload):
            entries.append({'step': step, **copy.deepcopy(payload)})
        with patch.object(server, '_log_debug', side_effect=log):
            asyncio.run(server._materialize_segment('test', None, current, 'root', tree, client))
        client._call.assert_called_once()
        self.assertEqual(current.event_history[-1]['event']['resources'][0]['amount'], 'половина')
        self.assertEqual(current.event_history[-1]['event']['closed_threads'], ['Кто отправитель?'])
        final = tree[next(iter(tree['root']['children'].values()))]['scene']
        self.assertEqual(final['ending'], 'victory')
        self.assertEqual(final['location_id'], 'harbor')
        final_log = next(e for e in entries if e['step'] == 'segment_prose_final')
        final_log['raw_scenes'][-1]['location_background_id'] = 'beach'
        with patch.object(DeepSeekClient, '_call', side_effect=AssertionError('No paid replay')):
            restored, _ = server._reconstruct_session(json.loads(json.dumps(entries)))
        restored_root = restored.story_tree[restored.tree_root_id]
        replayed = restored.story_tree[next(iter(restored_root['children'].values()))]['scene']
        self.assertEqual(replayed['ending'], 'victory')
        self.assertEqual(replayed['location_background_id'], 'beach')
        self.assertEqual(restored.event_history, current.event_history)
        self.assertEqual(restored.scene_conditions, current.scene_conditions)


if __name__ == '__main__':
    unittest.main()
