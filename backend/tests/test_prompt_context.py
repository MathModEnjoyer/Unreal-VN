"""Offline regression checks for history isolation and stable task prefixes."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from state_machine import StoryState


def state():
    s = StoryState(genre='детектив', tone='напряжённый', target_length=47)
    s.add_character('Алекс', 32, 'штурман', character_id='c1', is_protagonist=True)
    s.story_plan = {'ontology': 'рациональный', 'timeline': ['Путь начался 11 месяцев назад'],
                    'segments': [{'events': ['OUTLINE_ONLY_UNCHOSEN_POSSIBILITY']}]}
    s.add_context('Завязка: SOURCE_INPUT')
    s.record_event({'v': 3, 'participants': ['c1'], 'happening': {
        'subject': 'c1', 'verb': 'открывает', 'object': 'SOURCE_EVENT'}})
    s.pending_choice = 'SOURCE_DECISION'
    return s


EVENT = {'v': 3, 'participants': ['c1'], 'happening': {
    'subject': 'c1', 'verb': 'удерживает', 'object': 'NEW_EVENT'}}


class PromptContextTests(unittest.TestCase):
    def test_planning_and_review_reuse_history_but_prose_only_receives_segment(self):
        s = state()
        history = s.memory_prompt_block()
        client = DeepSeekClient(api_key='offline')
        client._call = Mock(side_effect=[
            json.dumps({'events': [EVENT]}), json.dumps({'replacements': []}),
            json.dumps({'scenes': [{'blocks': [{'type': 'narration', 'text': 'Груз остался на борту.'}]}]}),
            json.dumps({'events': [EVENT]}),
            json.dumps({'accepted': True, 'resulting_events': [EVENT]}),
        ])
        with patch('deepseek_client.get_visual_catalog') as catalog:
            catalog.return_value.prompt_catalog.return_value = 'PLANNER_CATALOG_SENTINEL\n'
            client.generate_segment_events(s, 2, 16, (12, 16))
            client.review_segment_events(s, [EVENT], final=False)
            client.generate_segment_prose(s, [EVENT], None)
            client.generate_custom_choice_events(s, 'SOURCE_DECISION')
            client.validate_custom_choice(s, 'A', 'B', [], [], 'SOURCE_DECISION')
        calls = client._call.call_args_list
        self.assertEqual(len(calls), 5)
        system = calls[0].kwargs['messages'][0]
        for i, call in enumerate(calls):
            messages = call.kwargs['messages']
            if i == 2:
                self.assertNotIn('SOURCE_EVENT', json.dumps(messages))
                self.assertNotIn('SOURCE_INPUT', json.dumps(messages))
                self.assertNotIn('SOURCE_DECISION', json.dumps(messages))
                self.assertIn('NEW_EVENT', messages[1]['content'])
                continue
            self.assertEqual(messages[0], system)
            self.assertTrue(messages[1]['content'].startswith(history + '\n'))
            self.assertEqual(messages[1]['content'].count('SOURCE_EVENT'), 1)
            self.assertNotIn('PLANNER_CATALOG_SENTINEL', system['content'])
            self.assertEqual('PLANNER_CATALOG_SENTINEL' in messages[1]['content'], i in (0, 3, 4))
        prose = json.dumps(calls[2].kwargs['messages'], ensure_ascii=False)
        review = json.dumps(calls[1].kwargs['messages'], ensure_ascii=False)
        self.assertNotIn('OUTLINE_ONLY_UNCHOSEN_POSSIBILITY', prose)
        self.assertNotIn('OUTLINE_ONLY_UNCHOSEN_POSSIBILITY', review)
        self.assertIn('OUTLINE_ONLY_UNCHOSEN_POSSIBILITY', calls[0].kwargs['messages'][1]['content'])

    def test_prose_prefix_is_stable_and_never_reads_history_or_plot_secrets(self):
        s = state()
        s.add_character('Ли', 38, 'врач', character_id='c2',
                        appearance_features={'gender': 'woman'},
                        story_purpose='SECRET_FUTURE_BETRAYAL', story_role='SECRET_ANTAGONIST')
        client = DeepSeekClient(api_key='offline')
        client._call = Mock(return_value=json.dumps({'scenes': [{'blocks': [
            {'type': 'narration', 'text': 'Груз остался на борту.'}]}]}))
        s.memory_prompt_block = Mock(side_effect=AssertionError('Prose must not read history'))
        s.to_prompt_block = Mock(side_effect=AssertionError('Prose must not read derived state'))
        for events in ([EVENT], ['LEGACY_CURRENT_EVENT']):
            with self.subTest(events=events):
                client.generate_segment_prose(s, events, None)
                before = copy.deepcopy(client._call.call_args.kwargs['messages'])
                s.record_event(EVENT)
                s.current_scene += 10
                s.segment_index += 1
                s.pending_choice = 'OTHER_CHOICE'
                s.location = 'OTHER_PLACE'
                s.story_plan['timeline'].append('SECRET_LATER_TIMELINE')
                client.generate_segment_prose(s, events, None)
                after = client._call.call_args.kwargs['messages']
                self.assertEqual(before, after)
                text = json.dumps(after, ensure_ascii=False)
                for hidden in ('SOURCE_EVENT', 'SOURCE_INPUT', 'SOURCE_DECISION', 'OTHER_CHOICE',
                               'OTHER_PLACE', 'SECRET_FUTURE_BETRAYAL', 'SECRET_ANTAGONIST',
                               'SECRET_LATER_TIMELINE', 'OUTLINE_ONLY_UNCHOSEN_POSSIBILITY'):
                    self.assertNotIn(hidden, text)
                cast = after[0]['content']
                self.assertIn('Ли', cast)
                self.assertIn('woman', cast)
                self.assertIn('"is_protagonist":true', cast)

    def test_dynamic_position_and_new_history_do_not_rewrite_shared_prefix(self):
        s = state()
        original_history = copy.deepcopy(s.event_history)
        client = DeepSeekClient(api_key='offline')
        before = client._story_messages(s, s.to_events_prompt_block(False), planning=True)
        history = s.memory_prompt_block()
        s.record_event(EVENT)
        s.current_scene += 1
        s.segment_index += 1
        s.pending_choice = 'OTHER_DECISION'
        s.location = 'OTHER_PLACE'
        s.add_line('c1', 'PRIVATE_PROSE_NOT_IN_MEMORY')
        after = client._story_messages(s, s.to_prompt_block(False))
        self.assertEqual(before[0], after[0])
        self.assertTrue(after[1]['content'].startswith(history))
        self.assertEqual(s.event_history[:len(original_history)], original_history)
        self.assertNotIn('PRIVATE_PROSE_NOT_IN_MEMORY', json.dumps(after))
        self.assertIn('NEW_EVENT', after[1]['content'])


if __name__ == '__main__':
    unittest.main()
