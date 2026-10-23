import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state_machine import StoryState, Character, AgeValidationError
from deepseek_client import DeepSeekClient
from story_events import normalize_event, bind_event, event_text
from prompts import build_segment_events_prompt, build_custom_choice_events_prompt
import server


def state():
    result = StoryState(genre='драма', tone='лёгкий', target_length=12)
    result.add_character('Анна', 30, role='смотритель', story_role='главный герой',
                         story_purpose='скрывает письмо', is_protagonist=True)
    return result


def event():
    return {'v': 2, 'participants': ['Анна'],
            'happening': {'subject': 'Анна', 'verb': 'находит', 'object': 'письмо'},
            'actions': [{'actor': 'Анна', 'verb': 'вскрывает', 'object': 'конверт'}],
            'reactions': [], 'results': ['Анна нашла письмо'],
            'numbers': [{'key': 'срок исчезновения капитана', 'value': '1 год назад'}],
            'conditions_change': {'time_of_day': 'night', 'atmosphere': {'weather': 'rain'}},
            'screen_events': [{'type': 'flash', 'source': 'lightning', 'intensity': 0.3}]}


class EventContractTests(unittest.TestCase):
    def test_contract_keeps_model_participants_without_cast_validation(self):
        for key in ['participants', 'actions']:
            raw = event()
            if key == 'participants':
                raw[key] = ['Борис']
            else:
                raw[key][0]['actor'] = 'Борис'
            normalized = normalize_event(raw, state().characters)
            self.assertEqual(normalized[key], raw[key])

    def test_memory_commits_only_with_accepted_scene_and_uses_plan(self):
        original = state()
        planned = normalize_event(event(), original.characters)
        scene = bind_event({'narration': 'Анна вскрыла конверт.', 'new_facts': ['выдумка прозы']}, planned)
        probe = copy.deepcopy(original)
        DeepSeekClient.apply_scene_to_state(probe, scene, 'Продолжить')
        self.assertEqual(original.event_history, [])
        self.assertFalse(hasattr(original, 'numeric_facts'))
        self.assertEqual(probe.event_history[-1]['event'], planned)
        self.assertIn('1 год назад', probe.memory_prompt_block())
        self.assertNotIn('выдумка прозы', probe.memory_prompt_block())
        self.assertEqual(probe.scene_conditions['time_of_day'], 'night')
        self.assertEqual(probe.player_choices, [])

    def test_legacy_numeric_sources_are_archived_without_a_canonical_index(self):
        current = state()
        current.record_event('Капитан исчез 6 лет назад')
        before = current.memory_prompt_block()
        scene = bind_event({'narration': 'Анна нашла письмо.'}, normalize_event(event(), current.characters))
        DeepSeekClient.apply_scene_to_state(current, scene)
        self.assertTrue(current.memory_prompt_block().startswith(before))
        self.assertIn('1 год назад', current.memory_prompt_block())
        self.assertFalse(hasattr(current, 'numeric_facts'))

    def test_prose_cannot_randomly_change_planned_weather(self):
        planned = normalize_event(event(), state().characters)
        bound = bind_event({'conditions_change': {'time_of_day': 'day'}, 'atmosphere': {'ambient': 'ash'}}, planned)
        self.assertEqual(bound['conditions_change']['time_of_day'], 'night')
        self.assertNotIn('atmosphere', bound)
        self.assertEqual(bound['screen_events'][0]['source'], 'lightning')
        staying = dict(planned)
        staying.pop('conditions_change')
        self.assertNotIn('conditions_change', bind_event({'conditions_change': {'time_of_day': 'day'}}, staying))

    def test_legacy_text_events_and_saves_still_work(self):
        self.assertEqual(normalize_event({'event': 'Письмо найдено.'}, {}), 'Письмо найдено.')
        current = state()
        old = current.to_dict()
        old['characters']['c1'].pop('story_role')
        old['characters']['c1'].pop('story_purpose')
        restored = StoryState.from_dict(old)
        self.assertEqual(restored.characters['c1'].role, 'смотритель')
        DeepSeekClient.apply_scene_to_state(restored, {'narration': 'Тихо.', 'new_facts': ['старый факт']})
        self.assertIn('старый факт', restored.memory_prompt_block())

    def test_real_choices_are_preserved_navigation_is_ignored(self):
        current = state()
        for choice in ['Продолжить', ' продолжить ', 'Начни историю с завязки: маяк', 'Сжечь письмо']:
            current.add_player_choice(choice)
        self.assertEqual(current.player_choices, ['Сжечь письмо'])

    def test_internal_roles_roundtrip_without_notebook_spoilers(self):
        current = StoryState.from_dict(state().to_dict())
        self.assertIn('скрывает письмо', current.characters_block())
        public = server._public_state(current)
        self.assertNotIn('скрывает письмо', json.dumps(public, ensure_ascii=False))
        self.assertNotIn('story_role', json.dumps(public))

    def test_generate_regular_and_custom_structures_each_in_one_call(self):
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=json.dumps({'events': [event()]}))
        events, _, _ = client.generate_segment_events(state(), 1, 4, None)
        self.assertEqual(events[0]['participants'], ['c1'])
        self.assertIn('вскрывает', event_text(events[0]))
        client._call.assert_called_once()
        client._call.reset_mock()
        client._call.return_value = json.dumps({'events': [event()]})
        custom, _ = client.generate_custom_choice_events(state(), 'Открыть письмо')
        self.assertEqual(custom[0]['numbers'][0]['value'], '1 год назад')
        client._call.assert_called_once()

    def test_prompts_build_and_request_structured_contract(self):
        for prompt in [build_segment_events_prompt('state', '', 1, 5, (3,5)),
                       build_custom_choice_events_prompt('state','C',10)]:
            self.assertIn('participants', prompt)
            self.assertNotIn('\"numbers\":', prompt)
            self.assertIn('happening', prompt)

    def test_structured_plan_materialization_and_jsonl_replay(self):
        current = state()
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(side_effect=[
            json.dumps({'events': [event()]}),
            json.dumps({'scenes': [{'narration': 'Анна нашла письмо и вскрыла конверт.',
                                    'location': 'Кабинет', 'location_id': 'office'}]}),
        ])
        client.moderate_segment_scenes = Mock(side_effect=AssertionError('Unexpected content moderation'))
        client.validate_segment_coherence = Mock(side_effect=AssertionError('Unexpected review'))
        tree, entries = {}, [
            {'step': 'plan', 'raw_plan': {}},
            {'step': 'characters', 'raw_characters': [vars(current.characters['c1'])]},
        ]
        def log(session, path, step, payload):
            entries.append({'step': step, **copy.deepcopy(payload)})
        async def run():
            root = await server._build_tree_events(current, client, tree, asyncio.Semaphore(1),
                                                   'test', None, 12, None, None)
            self.assertEqual(current.event_history, [])
            self.assertFalse(hasattr(current, 'numeric_facts'))
            current.story_tree = tree
            await server._materialize_segment('test', None, current, root, tree, client)
            self.assertIsInstance(tree[root]['scene'], dict)
            self.assertIn('Анна нашла письмо', current.memory_prompt_block())
        with patch.object(server, '_log_debug', side_effect=log):
            asyncio.run(run())
            replayed, _ = server._reconstruct_session(json.loads(json.dumps(entries)))
        self.assertEqual(client._call.call_count, 2)
        client.moderate_segment_scenes.assert_not_called()
        client.validate_segment_coherence.assert_not_called()
        self.assertIn('Анна нашла письмо', replayed.memory_prompt_block())
        self.assertIn('1 год назад', replayed.memory_prompt_block())
        self.assertEqual(replayed.scene_conditions['time_of_day'], 'night')

    def test_pending_continuation_uses_actual_accepted_facts(self):
        current = state()
        pending = copy.deepcopy(current)
        pending.planned_events_so_far = ['Письмо найдено.', '[Выбор героя] Сжечь письмо']
        tree = {}
        pending_id = server._make_pending_branch(tree, pending, 'Сжечь письмо', 12, None)
        current.story_tree = tree
        current.record_event('На конверте оказался красный воск')
        current.current_scene = 4
        current.build_all_branches = False
        async def build(branch, client, target, *args):
            self.assertIn('На конверте оказался красный воск', branch.memory_prompt_block())
            self.assertEqual(branch.current_scene, 4)
            self.assertIn('[Выбор героя] Сжечь письмо', branch.planned_events_so_far)
            target['new'] = {'segment_events': ['Письмо сожжено.'], 'children': {}}
            return 'new'
        with patch.object(server, '_client', return_value=Mock()), patch.object(server, '_build_tree_events', side_effect=build):
            result = asyncio.run(server._build_pending_branch('test', current, pending_id))
        self.assertEqual(result, pending_id)
        self.assertNotIn('new', tree)
        self.assertEqual(tree[pending_id]['segment_events'], ['Письмо сожжено.'])

    def test_economy_jsonl_restores_unbuilt_sides_with_their_decisions(self):
        choices = ['Сохранить письмо', 'Сжечь письмо']
        events = ['Письмо найдено.', 'Анна решает судьбу письма.']
        entries = [
            {'step': 'plan', 'raw_plan': {}, 'build_all_branches': False},
            {'step': 'characters', 'raw_characters': [vars(state().characters['c1'])]},
            {'step': 'segment_events', 'events': events, 'window': [2, 4],
             'ceiling_scene': 12, 'raw_pivot_choices': choices, 'origin_choice': None},
            {'step': 'segment_prose_final', 'events': events, 'segment_complete': True,
             'raw_scenes': [{'narration': text, 'location': 'Кабинет'} for text in events]},
        ]
        restored, _ = server._reconstruct_session(entries)
        first = restored.story_tree[restored.tree_root_id]
        pivot = restored.story_tree[next(iter(first['children'].values()))]
        self.assertFalse(restored.build_all_branches)
        self.assertEqual(set(pivot['children']), set(choices))
        for choice, child in pivot['children'].items():
            node = restored.story_tree[child]
            self.assertTrue(node['pending_branch'])
            self.assertIn(f'[Выбор героя] {choice}', node['pending_state']['planned_events_so_far'])


class AdultAgePolicyTests(unittest.TestCase):
    def test_creation_clamps_only_ages_below_eighteen(self):
        for raw, expected in [(0, 18), (16, 18), (17, 18), (18, 18), (25, 25), ('16', 18)]:
            with self.subTest(age=raw):
                current = state()
                added = current.add_character('Борис', raw, role='библиотекарь')
                self.assertEqual(added.age, expected)
                self.assertEqual(current.to_dict()['characters']['c2']['age'], expected)

    def test_invalid_or_missing_age_is_not_invented(self):
        for raw in [None, '', 'неизвестен', True]:
            with self.subTest(age=raw), self.assertRaises(AgeValidationError):
                Character('Борис', raw)

    def test_age_normalizes_in_snapshot_legacy_scene_and_jsonl(self):
        saved = state().to_dict()
        saved['characters']['c1']['age'] = 16
        restored = StoryState.from_dict(saved)
        self.assertEqual(restored.characters['c1'].age, 18)
        DeepSeekClient.apply_scene_to_state(restored, {
            'narration': 'Борис пришёл.',
            'new_character': {'name': 'Борис', 'age': 17, 'role': 'библиотекарь'}})
        self.assertEqual(restored.characters['c2'].age, 18)
        entries = [
            {'step': 'plan', 'raw_plan': {}},
            {'step': 'characters', 'raw_characters': list(saved['characters'].values())},
            {'step': 'segment_events', 'events': ['Анна пришла в кабинет.'],
             'ceiling_scene': 12, 'window': None, 'raw_pivot_choices': [], 'origin_choice': None},
        ]
        replayed, _ = server._reconstruct_session(entries)
        self.assertEqual(replayed.characters['c1'].age, 18)


class OnboardingOrderTests(unittest.TestCase):
    def test_fitting_and_clarification_skip_input_moderation(self):
        calls = []
        client = Mock()
        client.moderate_text.side_effect = AssertionError('Unexpected seed moderation')
        client.generate_genre_tone_fit.side_effect = lambda *args: (calls.append('fit') or ['драма'], ['лёгкий'])
        client.generate_clarifying_questions.side_effect = lambda *args: (calls.append('questions') or [])
        async def run():
            with patch.object(server, '_client', return_value=client):
                fit = await server.genre_tone_fit(server.GenreToneFitRequest(seed='Маяк', genre='драма'))
                questions = await server.clarify(server.ClarifyRequest(seed='Маяк', genre='драма', tone='лёгкий'))
                self.assertEqual(fit['genres'], ['драма'])
                self.assertEqual(questions['questions'], [])
        asyncio.run(run())
        self.assertEqual(calls, ['fit', 'questions'])
        client.moderate_text.assert_not_called()

    def test_direct_start_generates_plan_without_input_moderation(self):
        client = Mock()
        client.moderate_text.side_effect = AssertionError('Unexpected seed moderation')
        client.generate_story_plan.return_value = ({}, {})
        build = AsyncMock()
        async def run():
            with patch.object(server, '_client', return_value=client), \
                 patch.object(server, '_remember_session') as remember, \
                 patch.object(server, '_log_debug'), \
                 patch.object(server, '_build_tree_background', build), \
                 patch.dict(server.TREE_BUILD_TASKS, clear=True):
                result = await server.start_story(server.StartRequest(
                    seed='Маяк', genre='драма', tone='лёгкий', target_length=12))
                await server.TREE_BUILD_TASKS[result['session_id']]
                self.assertIn('Завязка: Маяк', remember.call_args.args[1].memory_prompt_block())
                self.assertIn('session_id', result)
        asyncio.run(run())
        client.generate_story_plan.assert_called_once()
        client.moderate_text.assert_not_called()
        build.assert_awaited_once()

    def test_economy_custom_choice_is_rejected_without_model_call(self):
        current = state()
        current.build_all_branches = False
        with patch.object(server, '_get_state', return_value=current), patch.object(server, '_client') as client:
            with self.assertRaises(server.HTTPException) as failure:
                asyncio.run(server._custom_choice_impl(server.CustomChoiceRequest(session_id='test', custom_text='Открыть письмо')))
        self.assertEqual(failure.exception.status_code, 409)
        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
