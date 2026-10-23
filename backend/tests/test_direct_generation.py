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


def state():
    current = StoryState(genre='драма', tone='лёгкий', target_length=12)
    current.add_character('Анна', 30)
    return current


class DirectGenerationTests(unittest.TestCase):
    def test_story_editors_are_removed_from_client(self):
        for name in ('validate_segment_coherence', 'check_branches_consistency',
                     'repair_scene', 'moderate_text',
                     'moderate_segment_scenes'):
            self.assertFalse(hasattr(DeepSeekClient, name), name)

    def test_short_response_keeps_choice_without_reviews_or_regeneration(self):
        current = state()
        events = ['Событие 1', 'Событие 2', 'Событие 3']
        choices = ['Остаться', 'Уйти']
        tree = {'root': {'segment_events': events, 'segment_choice_texts': choices,
                         'segment_children_ids': {'Остаться': 'a', 'Уйти': 'b'},
                         'origin_choice': 'Открыть дверь'}}
        current.story_tree = tree
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        # Neither a language mismatch nor an unregistered speaker triggers a review.
        client._call = Mock(return_value=json.dumps({'scenes': [{
            'narration': 'Some foreign text.', 'speaker': 'Борис', 'dialogue': 'Привет.',
            'new_character': {'name': 'Борис', 'age': 16},
        }]}))
        with patch.object(server, '_log_debug'):
            asyncio.run(server._materialize_segment('test', None, current, 'root', tree, client))
        client._call.assert_called_once()
        self.assertEqual(tree['root']['children'], {'Остаться': 'a', 'Уйти': 'b'})
        self.assertEqual(tree['root']['scene']['narration'], 'Some foreign text.')
        self.assertEqual(current.characters['c2'].age, 18)
        self.assertEqual(current.player_choices, ['Открыть дверь'])

    def test_extra_returned_scenes_are_not_discarded(self):
        current = state()
        tree = {'root': {'segment_events': ['Одно событие'], 'segment_choice_texts': None,
                         'segment_children_ids': {}, 'origin_choice': None}}
        current.story_tree = tree
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=json.dumps({'scenes': [
            {'narration': 'Первая сцена.'}, {'narration': 'Вторая сцена.'}]}))
        with patch.object(server, '_log_debug'):
            asyncio.run(server._materialize_segment('test', None, current, 'root', tree, client))
        client._call.assert_called_once()
        second = tree[next(iter(tree['root']['children'].values()))]
        self.assertEqual(second['scene']['narration'], 'Вторая сцена.')
        self.assertEqual(second['scene']['ending'], 'complete')

    def test_conflicting_source_deltas_are_kept_in_order_without_editorial_checks(self):
        current = state()
        events = [
            {'happening': {'verb': 'передаёт'}, 'entities': [{'name': 'ключ', 'holder': 'Анна'}]},
            {'happening': {'verb': 'получает'}, 'entities': [{'name': 'ключ', 'holder': 'Борис'}]},
        ]
        for source in events:
            current.record_event(source)
        self.assertEqual([r['event'] for r in current.event_history], events)
        self.assertLess(current.memory_prompt_block().index('Анна'), current.memory_prompt_block().index('Борис'))
        restored = StoryState.from_dict(current.to_dict())
        self.assertEqual(restored.event_history, current.event_history)
        for field in ('entities', 'resources', 'entity_issues', 'unregistered_speakers'):
            self.assertNotIn(field, restored.to_dict())

    def test_accepted_custom_action_plans_and_writes_without_story_review(self):
        current = state()
        current.tree_current_id = 'choice'
        current.story_tree = {'choice': {'children': {'A': 'a', 'B': 'b'}},
                              'a': {'segment_events': ['A']}, 'b': {'segment_events': ['B']}}
        client = DeepSeekClient(api_key='test')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(side_effect=[json.dumps({'accepted': True, 'resulting_events': ['Анна уходит.']}),
                                        json.dumps({'scenes': [{'narration': 'Анна ушла.'}]})])
        entries = []
        def log(session_id, path, step, payload):
            entries.append({'step': step, **copy.deepcopy(payload)})
        with (patch.object(server, '_get_state', return_value=current),
              patch.object(server, '_client', return_value=client),
              patch.object(server, '_log_debug', side_effect=log),
              patch.object(server, '_append_transcript'), patch.object(server, '_save_snapshot'),
              patch.object(server, '_scene_with_visuals', side_effect=lambda state, scene, **kw: scene)):
            response = asyncio.run(server._custom_choice_impl(server.CustomChoiceRequest(
                session_id='test', custom_text='Прыгнуть на Луну')))
        self.assertTrue(response['accepted'])
        self.assertTrue(response['finished'])
        self.assertEqual(client._call.call_count, 2)
        self.assertEqual(current.player_choices, ['Прыгнуть на Луну'])
        first_prompt = client._call.call_args_list[0].kwargs['messages'][1]['content']
        self.assertIn('Прыгнуть на Луну', first_prompt)
        self.assertIn('"accepted"', first_prompt)
        self.assertIn('clarifying_question', first_prompt)
        self.assertNotIn('validation_outcome', [e['step'] for e in entries])


if __name__ == '__main__':
    unittest.main()
