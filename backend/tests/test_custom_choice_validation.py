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
from state_machine import StoryState


OPTIONS = ['Подняться на верхнюю палубу', 'Найти лестницу к капитанскому мостику',
           'Попросить экипаж показать путь наверх']

def rejected(**fields):
    return {'accepted': False, 'reason': 'Герой не умеет летать.',
            'clarifying_question': 'Как вы хотите подняться наверх?',
            'clarifying_options': OPTIONS, 'resulting_events': [], **fields}

def current_state():
    state = StoryState(genre='драма', tone='лёгкий', target_length=12)
    state.add_character('Анна', 30)
    state.tree_current_id = state.tree_root_id = 'choice'
    state.current_scene = 4
    state.story_tree = {
        'choice': {'scene': {'narration': 'Анна стоит у трапа.'}, 'children': {'Остаться': 'a', 'Уйти': 'b'}},
        'a': {'segment_events': ['Анна ждёт у трапа.']},
        'b': {'segment_events': ['Анна возвращается в каюту.']},
    }
    return state


class CustomChoiceValidationTests(unittest.TestCase):
    def setUp(self):
        self.state = current_state()
        self.client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        self.client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, kwargs in [
            ('_get_state', {'side_effect': lambda sid: self.state}),
            ('_client', {'return_value': self.client}),
            ('_log_debug', {}), ('_append_transcript', {}), ('_save_snapshot', {}),
            ('_scene_with_visuals', {'side_effect': lambda state, scene, **kw: scene}),
        ]:
            self.stack.enter_context(patch.object(server, name, **kwargs))

    def request(self, text='Полететь наверх', proposal_id=None):
        return asyncio.run(server._custom_choice_impl(server.CustomChoiceRequest(
            session_id='test', custom_text=text, proposal_id=proposal_id)))

    def test_rejection_asks_question_without_advancing_or_committing_events(self):
        self.client._call = Mock(return_value=json.dumps(rejected(
            resulting_events=['Это событие не должно произойти.'])))
        before = copy.deepcopy(self.state.to_dict())
        response = self.request()
        self.client._call.assert_called_once()
        self.assertFalse(response['accepted'])
        self.assertEqual(response['clarifying_options'], OPTIONS)
        self.assertTrue(response['clarifying_question'])
        self.assertTrue(response['proposal_id'])
        self.assertEqual(self.state.tree_current_id, 'choice')
        self.assertEqual(self.state.event_history, before['event_history'])
        self.assertEqual(self.state.player_choices, [])
        self.assertEqual(set(self.state.story_tree), set(before['story_tree']))
        self.assertEqual(self.state.story_tree['choice']['children'], before['story_tree']['choice']['children'])
        prompt = self.client._call.call_args.kwargs['messages'][1]['content']
        self.assertIn('Анна ждёт у трапа.', prompt)
        self.assertIn('Анна возвращается в каюту.', prompt)
        self.assertIn('минимальное исправление', prompt)

    def test_confirmed_correction_skips_validation_and_survives_snapshot(self):
        self.client._call = Mock(return_value=json.dumps(rejected()))
        proposal = self.request()
        self.state = StoryState.from_dict(json.loads(json.dumps(self.state.to_dict())))
        self.client._call = Mock(side_effect=[
            json.dumps({'events': ['Анна поднялась на палубу.']}),
            json.dumps({'scenes': [{'narration': 'Анна поднялась на палубу.'}]}),
        ])
        with patch.object(self.client, 'validate_custom_choice', side_effect=AssertionError('Repeated validation')):
            response = self.request(OPTIONS[0], proposal['proposal_id'])
        self.assertTrue(response['accepted'])
        self.assertEqual(self.client._call.call_count, 2)
        self.assertEqual(self.state.player_choices, [OPTIONS[0]])
        self.assertNotIn('Полететь наверх', self.state.player_choices)
        self.assertEqual(self.state.story_tree[self.state.tree_current_id]['scene']['narration'],
                         'Анна поднялась на палубу.')
        self.assertNotIn('custom_choice_proposal', self.state.story_tree['choice'])

    def test_valid_action_keeps_original_intention_and_combines_validation_with_planning(self):
        self.client._call = Mock(side_effect=[
            json.dumps({'accepted': True, 'effective_action': 'Подменить действие',
                        'resulting_events': ['Анна зовёт капитана.']}),
            json.dumps({'scenes': [{'narration': 'Анна позвала капитана.'}]}),
        ])
        result = self.request('Позвать капитана')
        self.assertTrue(result['accepted'])
        self.assertEqual(self.client._call.call_count, 2)
        self.assertEqual(self.state.player_choices, ['Позвать капитана'])

    def test_stale_or_edited_correction_never_calls_model(self):
        self.client._call = Mock(return_value=json.dumps(rejected()))
        response = self.request()
        self.client._call.reset_mock()
        for text, proposal in [(OPTIONS[0], 'obsolete'), ('Не предложенное действие', response['proposal_id'])]:
            with self.subTest(text=text), self.assertRaises(server.HTTPException) as failure:
                self.request(text, proposal)
            self.assertEqual(failure.exception.status_code, 409)
        self.client._call.assert_not_called()
        self.assertEqual(self.state.tree_current_id, 'choice')

    def test_malformed_suggestions_or_missing_events_are_not_presented_as_success(self):
        for payload in [rejected(clarifying_options=OPTIONS[:2]),
                        rejected(clarifying_options=[OPTIONS[0]] * 3),
                        rejected(clarifying_question=''),
                        {'accepted': True, 'resulting_events': []}, {'resulting_events': ['Событие']}]:
            with self.subTest(payload=payload):
                self.client._call = Mock(return_value=json.dumps(payload))
                with self.assertRaises(server.HTTPException):
                    self.request()
                self.assertEqual(self.state.tree_current_id, 'choice')
                self.assertNotIn('custom_choice_proposal', self.state.story_tree['choice'])


if __name__ == '__main__':
    unittest.main()
