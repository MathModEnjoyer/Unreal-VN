"""Deterministic API fixture. Never connects to DeepSeek or reads a real key."""
import json
from deepseek_client import DeepSeekClient
from prompts import choice_windows


class ScenarioClient(DeepSeekClient):
    calls = []

    def __init__(self, **kwargs):
        super().__init__(api_key='test-fixture', **kwargs)

    def generate_segment_events(self, state, start_scene, ceiling_scene, window):
        self.coordinates = start_scene, ceiling_scene, window
        return super().generate_segment_events(state, start_scene, ceiling_scene, window)

    def generate_segment_prose(self, state, events, choice_texts):
        self.events = events
        return super().generate_segment_prose(state, events, choice_texts)

    def _call(self, messages, temperature, state=None, json_mode=True, *, operation='unspecified'):
        self.calls.append(operation)
        if state:
            state.record_usage({'prompt_tokens': 100, 'completion_tokens': 40,
                                'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 20})
        if operation == 'generate_story_plan':
            length = state.target_length if state else 47
            data = {'segments': [{'places': ['улица'], 'events': ['поиск свидетеля']}
                                 for _ in range(len(choice_windows(length)) + 1)],
                    'initial_conditions': {'time_of_day': 'night', 'atmosphere': {'weather': 'rain'}}}
        elif operation == 'generate_characters':
            data = {'characters': [
                {'id': 'c1', 'name': 'Анна', 'age': 17, 'is_protagonist': True, 'role': 'врач'},
                {'id': 'c2', 'name': 'Анна', 'age': 29, 'role': 'капитан'},
                {'id': 'c3', 'name': 'Борис', 'age': 40, 'role': 'управляющий'}],
                'relationships': [{'between': ['c1', 'c2'], 'type': 'знакомые', 'attitude': 'доверие'}]}
        elif operation == 'generate_segment_events':
            start, ceiling, window = self.coordinates
            data = {'events': [self.event(i, state.segment_index) for i in range(start, ceiling + 1)]}
            if window:
                data['events'][-1]['choices'] = ['Осмотреть дверь', 'Найти свидетеля']
            else:
                data['events'][-1]['ending'] = 'victory'
        elif operation == 'review_segment_events':
            data = {'replacements': []}
        elif operation == 'generate_segment_prose':
            data = {'scenes': [{'narration': f"Сцена {i + 1}. Анна и Борис изучают улику. "
                               "На столе лежит письмо; следы ведут к двери.",
                               'speaker_id': 'c2', 'dialogue': 'Продолжим поиски.'}
                              for i, event in enumerate(self.events)]}
        elif operation == 'validate_custom_choice':
            if 'Луну' in messages[-1]['content']:
                data = {'accepted': False, 'reason': 'Это действие здесь невозможно.',
                        'clarifying_question': 'Искать путь поближе?',
                        'clarifying_options': ['Осмотреть крышу', 'Найти лестницу', 'Изучить карту']}
            else:
                data = {'accepted': True, 'resulting_events': [self.event(99, 2) | {'ending': 'victory'}]}
        elif operation == 'generate_custom_choice_events':
            data = {'events': [self.event(99, 2) | {'ending': 'victory'}]}
        elif operation == 'generate_clarifying_questions':
            data = {'questions': [{'question': 'Где начинается история?', 'options': ['В городе', 'В деревне']}]}
        elif operation == 'generate_genre_tone_fit':
            data = {'genres': ['детектив', 'драма'], 'tones': ['мрачный', 'лёгкий']}
        elif operation == 'generate_premise_suggestions':
            data = {'premises': [{'seed': 'В городе исчез свидетель', 'label': 'Исчезновение'}]}
        else:
            raise AssertionError(f'Unexpected fixture operation: {operation}')
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def event(number, segment):
        return {'v': 3, 'participants': ['c1', 'c2', 'c3'],
                'happening': {'subject': 'c2', 'verb': 'изучает', 'object': f'улику {segment}:{number}'},
                'expressions': {'c2': 'smile' if number % 2 else 'tension'}}
