import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from deepseek_client import DeepSeekClient
from deepseek_logging import write_record
from state_machine import StoryState


class DeepSeekLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.key = 'sk-private-test-secret'
        self.client = DeepSeekClient(api_key=self.key, session_id='session-1', log_dir=self.directory)
        self.messages = [{'role': 'user', 'content': 'Сырой журнал\nНовый сегмент'}]
        self.data = {'choices': [{'message': {'content': '{"scenes": []}'}}],
                     'usage': {'prompt_tokens': 120, 'completion_tokens': 30,
                               'prompt_cache_hit_tokens': 100, 'prompt_cache_miss_tokens': 20}}

    def records(self):
        return [json.loads(line) for path in self.directory.glob('*.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]

    def response(self, status=200, *, body=None):
        return httpx.Response(status, request=httpx.Request('POST', 'https://api.deepseek.com/chat/completions'),
                              text=body if body is not None else json.dumps(self.data, ensure_ascii=False))

    def call(self, response=None, side_effect=None, **kwargs):
        with patch('deepseek_client.httpx.Client') as factory:
            post = factory.return_value.__enter__.return_value.post
            post.return_value = response if response is not None else self.response()
            post.side_effect = side_effect
            result = self.client._call(self.messages, temperature=0.6, operation='test_operation', **kwargs)
            return result, factory, post

    def test_full_request_response_usage_context_and_request_before_send(self):
        state = StoryState(genre='драма', tone='мрачный', target_length=24)
        state.segment_index = 2

        def send(*args, **kwargs):
            records = self.records()
            self.assertEqual([r['event'] for r in records], ['request'])
            self.assertEqual(records[0]['payload'], kwargs['json'])
            return self.response()

        result, factory, post = self.call(side_effect=send, state=state)
        request, response = self.records()
        self.assertEqual(result, self.data['choices'][0]['message']['content'])
        self.assertEqual(request['request_id'], response['request_id'])
        self.assertEqual(request['attempt'], 1)
        self.assertEqual(response['attempt'], 1)
        self.assertEqual(request['generation_id'], response['generation_id'])
        self.assertEqual(request['session_id'], 'session-1')
        self.assertEqual(request['operation'], 'test_operation')
        self.assertEqual(request['story']['segment_index'], 2)
        self.assertEqual(request['story']['current_scene'], 0)
        self.assertEqual(request['payload']['messages'], self.messages)
        self.assertEqual(request['payload']['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', request['payload'])
        self.assertNotIn('operation', post.call_args.kwargs['json'])
        self.assertEqual(json.loads(response['response_body']), self.data)
        self.assertEqual(response['status_code'], 200)
        self.assertGreaterEqual(response['elapsed_ms'], 0)
        self.assertEqual(response['usage'], {'input_tokens': 120, 'output_tokens': 30,
                         'cache_hit_tokens': 100, 'cache_miss_tokens': 20})
        self.assertEqual(state.total_api_calls, 1)
        self.assertEqual(factory.call_args.kwargs['headers']['Authorization'], f'Bearer {self.key}')
        self.assertEqual(post.call_count, 1)

    def test_stateless_menu_request_is_logged_with_operation(self):
        self.client.session_id = None
        data = {'choices': [{'message': {'content': json.dumps({'questions': []})}}]}
        with patch('deepseek_client.httpx.Client') as factory:
            factory.return_value.__enter__.return_value.post.return_value = self.response(body=json.dumps(data))
            self.client.generate_clarifying_questions('История', 'драма', 'лёгкий')
        request, response = self.records()
        self.assertIsNone(request['session_id'])
        self.assertEqual(request['operation'], 'generate_clarifying_questions')
        self.assertNotIn('story', request)
        self.assertEqual(response['usage']['cache_hit_tokens'], 0)

    def test_http_error_preserves_body_status_and_original_exception(self):
        response = self.response(429, body='provider error')
        with self.assertRaises(httpx.HTTPStatusError) as raised:
            self.call(response=response)
        request, error = self.records()
        self.assertIs(raised.exception.response, response)
        self.assertEqual(error['request_id'], request['request_id'])
        self.assertEqual(error['status_code'], 429)
        self.assertEqual(error['response_body'], 'provider error')
        self.assertEqual(error['error']['type'], 'HTTPStatusError')

    def test_timeout_retry_has_separate_ids_and_clears_stale_usage(self):
        timeout = httpx.ReadTimeout('read timed out')
        self.client._last_usage = {'input_tokens': 999}
        with self.assertRaises(httpx.ReadTimeout) as raised:
            self.call(side_effect=timeout)
        self.assertIs(raised.exception, timeout)
        self.assertEqual(self.client._last_usage, {})
        self.call()
        records = self.records()
        self.assertEqual([r['event'] for r in records], ['request', 'error', 'request', 'response'])
        self.assertNotEqual(records[0]['request_id'], records[2]['request_id'])
        self.assertIsNone(records[1]['status_code'])
        self.assertEqual(records[1]['usage'], {})

    def test_bad_api_json_and_missing_choices_are_logged(self):
        for body, exception in [('invalid json', json.JSONDecodeError), ('{}', KeyError)]:
            with self.subTest(body=body), self.assertRaises(exception):
                self.call(response=self.response(body=body))
        records = self.records()
        self.assertEqual([r['event'] for r in records], ['request', 'error', 'request', 'error'])
        self.assertEqual(records[1]['response_body'], 'invalid json')
        self.assertEqual(records[3]['response_body'], '{}')

    def test_key_is_redacted_from_prompt_body_and_error_without_mutating_request(self):
        self.messages[0]['content'] += self.key
        self.data['choices'][0]['message']['content'] = self.key
        result, _, post = self.call()
        self.assertEqual(result, self.key)
        self.assertIn(self.key, post.call_args.kwargs['json']['messages'][0]['content'])
        with self.assertRaises(httpx.ReadTimeout):
            self.call(side_effect=httpx.ReadTimeout('provider echoed ' + self.key))
        with self.assertRaises(httpx.HTTPStatusError):
            self.call(response=self.response(401, body='bad key ' + self.key))
        text = '\n'.join(p.read_text(encoding='utf-8') for p in self.directory.glob('*.jsonl'))
        self.assertNotIn(self.key, text)
        self.assertNotIn('Authorization', text)
        self.assertIn('[REDACTED]', text)

    def test_logging_io_failure_does_not_change_success_or_repeat_send(self):
        with patch('deepseek_logging.Path.open', side_effect=OSError('disk full')), \
             self.assertLogs('deepseek_logging', level='WARNING'):
            result, _, post = self.call()
        self.assertEqual(result, self.data['choices'][0]['message']['content'])
        self.assertEqual(post.call_count, 1)

    def test_logging_io_failure_preserves_original_api_error(self):
        failure = httpx.ConnectError('offline')
        with patch('deepseek_logging.Path.open', side_effect=OSError('disk full')), \
             self.assertLogs('deepseek_logging', level='WARNING'), \
             self.assertRaises(httpx.ConnectError) as raised:
            self.call(side_effect=failure)
        self.assertIs(raised.exception, failure)

    def test_concurrent_writes_produce_complete_json_lines(self):
        def write(number):
            write_record({'event': 'request', 'number': number, 'payload': 'текст\n' * 300},
                         api_key=self.key, log_dir=self.directory)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(100)))
        records = self.records()
        self.assertEqual(len(records), 100)
        self.assertEqual({r['number'] for r in records}, set(range(100)))

    def test_server_client_forwards_session(self):
        import server
        with patch.object(server.config_store, 'load_config', return_value={'deepseek_api_key': self.key}), \
             patch.object(server, 'DeepSeekClient') as factory:
            server._client(session_id='story-session')
        self.assertEqual(factory.call_args.kwargs['session_id'], 'story-session')


if __name__ == '__main__':
    unittest.main()
