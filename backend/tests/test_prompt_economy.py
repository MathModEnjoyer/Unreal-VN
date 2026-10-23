"""Cache layout and lossless event-to-prose binding; no external requests."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from prompts import PROSE_RULES, MODERN_PROSE_METADATA
from state_machine import StoryState


EVENT = {'v': 3, 'participants': ['c1'], 'happening': {
    'subject': 'c1', 'verb': 'осматривает', 'object': 'закрытый люк'}}
SCENE = {'blocks': [{'type': 'narration', 'text': 'Люк оставался закрытым.'}]}


class PromptEconomyTests(unittest.TestCase):
    def setUp(self):
        self.state = StoryState(genre='детектив', tone='напряжённый', target_length=47)
        self.char = self.state.add_character('Анна', 32, 'врач', character_id='c1',
            is_protagonist=True, appearance_features={'gender': 'woman'})
        self.client = DeepSeekClient(api_key='offline')
        self.client._call = Mock(return_value=json.dumps({'scenes': [SCENE]}))

    def test_changing_decision_and_segment_preserve_full_prose_system_prefix(self):
        self.client.generate_segment_prose(self.state, [EVENT], ('Ждать', 'Уйти'))
        before = copy.deepcopy(self.client._call.call_args.kwargs['messages'])
        event = copy.deepcopy(EVENT)
        event['happening']['object'] = 'наружная панель'
        self.client.generate_segment_prose(self.state, [event], ('Осмотреть', 'Спрятаться'))
        after = self.client._call.call_args.kwargs['messages']
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        for messages in (before, after):
            self.assertIn(PROSE_RULES, messages[0]['content'])
            self.assertIn(MODERN_PROSE_METADATA, messages[0]['content'])
            self.assertNotIn(PROSE_RULES, messages[1]['content'])
            self.assertNotIn(MODERN_PROSE_METADATA, messages[1]['content'])
            self.assertEqual(json.dumps(messages, ensure_ascii=False).count(PROSE_RULES[:50]), 1)

    def test_planner_has_gender_and_protagonist_even_with_generic_sprite(self):
        self.char.sprite_appearance = 'Человек в рабочем костюме'
        block = self.state.characters_block()
        self.assertIn('Пол: woman', block)
        self.assertIn('ГГ: True', block)
        self.assertIn(self.char.sprite_appearance, block)
        self.char.revealed = True
        self.state.current_scene += 1
        self.assertEqual(block, self.state.characters_block())

    def test_bad_prose_never_shifts_or_discards_events(self):
        invalid = ([], [SCENE], [SCENE, SCENE, SCENE], [None, SCENE],
                   [{}, SCENE], [SCENE, {'blocks': []}])
        for scenes in invalid:
            with self.subTest(scenes=scenes):
                self.client._call.return_value = json.dumps({'scenes': scenes})
                with self.assertRaises(KeyError):
                    self.client.generate_segment_prose(self.state, [EVENT, EVENT], None)
                self.assertEqual(self.state.event_history, [])
        self.client._call.return_value = json.dumps({'scenes': [SCENE, SCENE]})
        result, _ = self.client.generate_segment_prose(self.state, [EVENT, EVENT], None)
        self.assertEqual(len(result), 2)


if __name__ == '__main__':
    unittest.main()
