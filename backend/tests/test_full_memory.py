import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from state_machine import StoryState
from story_events import bind_event, normalize_event


def state():
    current = StoryState(genre='драма', tone='лёгкий', target_length=120)
    current.add_character('Анна', 30)
    return current


def event(text='письмо', **deltas):
    return {'v': 3, 'participants': ['Анна'],
            'happening': {'subject': 'Анна', 'verb': 'изучает', 'object': text}, **deltas}


class FullMemoryTests(unittest.TestCase):
    def test_full_source_without_separate_facts_or_numbers_or_old_prose(self):
        source = event(('Капитан исчез 123 дня назад: ' + 'подробность ' * 100).strip(),
                       actions=[{'actor': 'Анна', 'verb': 'узнаёт',
                                 'object': 'Сигнал каждые 40 секунд ' + 'а' * 700}],
                       results=['Лишний пересказ'], numbers=[{'key': 'срок', 'value': '123 дня'}])
        planned = normalize_event(source, {}, default_version=3)
        self.assertEqual(planned['happening'], source['happening'])
        self.assertEqual(planned['actions'], source['actions'])
        for field in ('results', 'numbers', 'facts'):
            self.assertNotIn(field, planned)
        current = state()
        DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'PRIVATE_PROSE'}, planned))
        self.assertEqual(current.event_history[0]['event'], planned)
        self.assertNotIn('PRIVATE_PROSE', current.to_prompt_block())
        for field in ('facts', 'numeric_facts', 'memory_log', 'entities', 'resources', 'world_rules', 'open_threads'):
            self.assertNotIn(field, current.to_dict())
            self.assertFalse(hasattr(current, field))

    def test_seventy_events_keep_all_details_choices_and_deltas_without_calls(self):
        current = state()
        for i in range(70):
            planned = event(f'Срок {i}: {i} дней ' + 'а' * 600,
                            entities=[{'name': f'предмет {i}', 'holder': 'Анна'}],
                            resources=[{'name': f'ресурс {i}', 'amount': str(i)}],
                            world_rules=[f'Правило {i}'], new_open_threads=[f'Нить {i}'])
            DeepSeekClient.apply_scene_to_state(current, bind_event(
                {'narration': f'PRIVATE_PROSE {i}'}, planned), f'Решение {i}')
        events = [r['event'] for r in current.event_history if 'event' in r]
        self.assertEqual(len(events), 70)
        self.assertEqual(len(current.player_choices), 70)
        self.assertEqual(len(events[0]['entities']), 1)
        self.assertEqual(events[-1]['resources'][0]['amount'], '69')
        for prompt in (current.to_prompt_block(), current.to_events_prompt_block()):
            self.assertIn(events[0]['happening']['object'], prompt)
            self.assertIn('Решение 0', prompt)
            self.assertIn('Правило 0', prompt)
            self.assertNotIn('PRIVATE_PROSE', prompt)

    def test_prefix_only_grows_and_nested_source_is_copied(self):
        current = state()
        source = event('ключ', entities=[{'name': 'ключ', 'holder': 'Анна'}])
        DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'PRIVATE'}, source), 'Взять ключ')
        before, records = current.memory_prompt_block(), copy.deepcopy(current.event_history)
        source['entities'][0]['holder'] = 'Подмена'
        DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'PRIVATE'},
            event('ключ', entities=[{'name': 'ключ', 'holder': 'Борис'}])))
        self.assertTrue(current.memory_prompt_block().startswith(before))
        self.assertEqual(current.event_history[:len(records)], records)
        self.assertEqual([r['event']['entities'][0]['holder'] for r in current.event_history if 'event' in r],
                         ['Анна', 'Борис'])

    def test_only_planning_requests_share_history_and_prose_is_segment_only(self):
        current = state()
        current.record_event(event('Секретная исходная улика'))
        current.add_player_choice('Сохранить письмо')
        current.add_line('(нарратор)', 'PRIVATE_OLD_NARRATION')
        current.add_line('Анна', 'PRIVATE_OLD_DIALOGUE')
        history = current.memory_prompt_block()
        client = DeepSeekClient(api_key='test')
        client._call = Mock(side_effect=[json.dumps({'events': ['Продолжение']}),
            json.dumps({'scenes': [{'narration': 'Продолжение'}]}),
            json.dumps({'accepted': True, 'resulting_events': ['Продолжение']}),
            json.dumps({'events': ['Продолжение']})])
        client.generate_segment_events(current, 1, 4, None)
        client.generate_segment_prose(current, ['Продолжение'], None)
        client.validate_custom_choice(current, 'Ждать', 'Уйти', [], [], 'Позвать капитана')
        client.generate_custom_choice_events(current, 'Позвать капитана')
        systems = []
        for call in client._call.call_args_list:
            messages = call.kwargs['messages']
            prompt = messages[1]['content']
            self.assertNotIn('PRIVATE_OLD_', json.dumps(messages))
            if call.kwargs['operation'] == 'generate_segment_prose':
                self.assertNotIn('Секретная исходная улика', json.dumps(messages, ensure_ascii=False))
                self.assertNotIn('Сохранить письмо', json.dumps(messages, ensure_ascii=False))
                self.assertIn('Продолжение', prompt)
                continue
            systems.append(messages[0])
            self.assertTrue(prompt.startswith(history + '\n'))
            self.assertEqual(prompt.count('Секретная исходная улика'), 1)
            self.assertEqual(prompt.count('Сохранить письмо'), 1)
        self.assertTrue(all(system == systems[0] for system in systems))

    def test_snapshots_and_sibling_branches_do_not_share_history(self):
        current = state()
        current.record_event(event('Письмо найдено'))
        snapshot = json.loads(json.dumps(current.to_dict()))
        first, second = StoryState.from_dict(snapshot), StoryState.from_dict(snapshot)
        self.assertEqual(first.memory_prompt_block(), current.memory_prompt_block())
        first.event_history[0]['event']['happening']['object'] = 'Только первая ветка'
        first.record_event(event('Новая улика'))
        self.assertNotIn('Только первая ветка', second.memory_prompt_block())
        self.assertNotIn('Новая улика', second.memory_prompt_block())
        self.assertEqual(snapshot['event_history'], current.event_history)

    def test_old_saves_import_source_once_without_recreating_fact_indexes(self):
        snapshot = state().to_dict()
        snapshot.pop('event_history')
        snapshot.update(facts=['(ранее) Сводка', 'Полный исходный факт'],
                        numeric_facts={'срок': '7 дней'}, player_choices=['Открыть дверь'],
                        entities={'ключ': {'holder': 'Анна'}},
                        resources={'вода': {'amount': 'полный'}},
                        world_rules=[{'rule': 'Нет связи'}], open_threads=['Кто капитан?'],
                        recent_lines=[{'speaker': 'Анна', 'text': 'PRIVATE_OLD_PROSE'}],
                        memory_log=[{'scene': 1, 'kind': 'number_observation', 'key': '0',
                                     'value': {'name': 'срок', 'value': '8 дней'}}])
        restored = StoryState.from_dict(snapshot)
        for value in ('(ранее) Сводка', 'Полный исходный факт', '7 дней', '8 дней', 'Открыть дверь', 'ключ', 'Нет связи', 'Кто капитан?'):
            self.assertIn(value, restored.memory_prompt_block())
        self.assertNotIn('PRIVATE_OLD_PROSE', restored.to_prompt_block())
        self.assertEqual(StoryState.from_dict(restored.to_dict()).event_history, restored.event_history)
        for field in ('facts', 'numeric_facts', 'memory_log', 'entities', 'resources', 'world_rules', 'open_threads'):
            self.assertNotIn(field, restored.to_dict())

    def test_future_plans_and_other_branches_never_enter_history(self):
        current = state()
        current.record_event(event('Уже произошло'))
        before = current.memory_prompt_block()
        current.planned_events_so_far = [json.dumps(event('Только будущий план'), ensure_ascii=False)]
        current.story_tree = {'other': {'segment_events': [event('Другая ветка')]}}
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'events': [event('Ещё не применено')]}))
        client.generate_segment_events(current, 1, 4, None)
        self.assertEqual(current.memory_prompt_block(), before)
        prompt = client._call.call_args.kwargs['messages'][1]['content']
        self.assertIn('Только будущий план', prompt)
        self.assertNotIn('Другая ветка', prompt)

    def test_legacy_fields_stay_in_raw_event_without_number_extraction(self):
        current = state()
        legacy = {'v': 2, 'participants': ['Анна'], 'happening': {'verb': 'ждёт'},
                  'results': ['Капитан исчез 7 дней назад'],
                  'numbers': [{'key': 'срок', 'value': '7 дней'}]}
        DeepSeekClient.apply_scene_to_state(current, bind_event({'narration': 'PRIVATE'}, legacy))
        self.assertEqual(current.event_history[0]['event'], legacy)
        self.assertIn('7 дней', current.memory_prompt_block())
        self.assertFalse(hasattr(current, 'numeric_facts'))

    def test_no_summary_calls_and_real_cache_usage_still_recorded(self):
        for method in ('_compact_facts', '_compact_choices'):
            self.assertFalse(hasattr(DeepSeekClient, method))
        current = state()
        current.record_usage({'prompt_tokens': 300, 'completion_tokens': 50,
                              'prompt_cache_hit_tokens': 256, 'prompt_cache_miss_tokens': 44})
        self.assertEqual(current.total_input_tokens, 300)
        self.assertEqual(current.total_cache_hit_tokens, 256)
        self.assertEqual(current.total_cache_miss_tokens, 44)
        self.assertEqual(current.total_api_calls, 1)
