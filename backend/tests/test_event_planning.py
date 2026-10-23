import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from prompts import choice_windows
from state_machine import StoryState


def response(events, choices=None):
    items = [{'event': text} for text in events]
    if choices:
        items[-1]['choices'] = choices
    return json.dumps({'events': items}, ensure_ascii=False)


class EventPlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        with patch('pathlib.Path.home', return_value=Path(cls.home.name)):
            import server
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def build(self, client, state, window=None):
        tree = {}
        ceiling = window[1] if window else state.target_length
        with patch.object(self.server, '_log_debug'):
            root = asyncio.run(self.server._build_tree_events(
                state, client, tree, asyncio.Semaphore(1), 'test-events', None,
                ceiling, window, None,
            ))
        return tree, tree[root]

    def client(self, **mock_options):
        client = DeepSeekClient(api_key='test-key')
        client._call = Mock(**mock_options)
        return client

    def state(self, full=True):
        state = StoryState(genre='драма', tone='лёгкий', target_length=12)
        state.build_all_branches = full
        return state

    def test_valid_events_are_accepted_after_one_model_call(self):
        events = ['Письмо найдено.', 'История закончилась.']
        client = self.client(return_value=response(events))
        _, node = self.build(client, self.state())
        client._call.assert_called_once()
        self.assertEqual(node['segment_events'], events)
        self.assertIsNone(node['scene'])
        self.assertEqual(node['segment_children_ids'], {})

    def test_invalid_response_is_retried_without_plan_review(self):
        events = ['История закончилась.']
        client = self.client(side_effect=['{"events":[]}', response(events)])
        _, node = self.build(client, self.state())
        self.assertEqual(client._call.call_count, 2)
        self.assertEqual(node['segment_events'], events)
        prompts = [call.kwargs['messages'] for call in client._call.call_args_list]
        self.assertEqual(prompts[0], prompts[1])

    def test_network_retry_limit_keeps_graceful_failure(self):
        client = self.client(side_effect=httpx.ReadTimeout('test timeout'))
        _, node = self.build(client, self.state())
        self.assertEqual(client._call.call_count, 3)
        self.assertEqual(node['scene']['ending'], 'failure')
        self.assertEqual(node['children'], {})

    def test_normal_mode_does_not_build_future_branches_before_current_prose(self):
        choices = ['Сохранить письмо', 'Сжечь письмо']
        client = self.client(side_effect=[
            response(['Письмо найдено.', 'Нужно решить судьбу письма.'], choices),
            response(['Письмо сохранено.']), response(['Письмо сожжено.']),
        ])
        client.check_branches_consistency = Mock(return_value=([], {}))
        tree, root = self.build(client, self.state(), choice_windows(12)[0])
        self.assertEqual(client._call.call_count, 1)
        client.check_branches_consistency.assert_not_called()
        self.assertEqual(root['segment_choice_texts'], choices)
        self.assertEqual(set(root['segment_children_ids']), set(choices))
        self.assertEqual(len(tree), 3)
        for child in root['segment_children_ids'].values():
            self.assertNotIn('segment_events', tree[child])
            self.assertTrue(tree[child]['pending_branch'])

    def test_economy_mode_keeps_both_branches_pending_until_choice(self):
        choices = ['Сохранить письмо', 'Сжечь письмо']
        client = self.client(side_effect=[
            response(['Письмо найдено.', 'Нужно решить судьбу письма.'], choices),
            response(['Письмо сохранено.']),
        ])
        client.check_branches_consistency = Mock()
        tree, root = self.build(client, self.state(False), choice_windows(12)[0])
        self.assertEqual(client._call.call_count, 1)
        client.check_branches_consistency.assert_not_called()
        first, second = (tree[root['segment_children_ids'][choice]] for choice in choices)
        self.assertTrue(first['pending_branch'])
        self.assertEqual(first['origin_choice'], choices[0])
        self.assertTrue(second['pending_branch'])
        self.assertEqual(second['origin_choice'], choices[1])

    def test_choice_before_allowed_window_is_still_discarded(self):
        client = self.client(side_effect=[
            response(['Слишком ранний выбор.'], ['Первый вариант', 'Второй вариант']),
            response(['История закончилась.']),
        ])
        _, root = self.build(client, self.state(), choice_windows(12)[0])
        self.assertEqual(client._call.call_count, 1)
        self.assertIsNone(root['segment_choice_texts'])
        self.assertEqual(list(root['segment_children_ids']), ['Продолжить'])


if __name__ == '__main__':
    unittest.main()
