import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import server
from deepseek_client import DeepSeekClient
from deepseek_logging import current_attempt
from state_machine import StoryState


class AttemptLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.client = DeepSeekClient(api_key='sk-test-key', log_dir=self.directory)

    def requests(self):
        records = [json.loads(line) for p in self.directory.glob('*.jsonl')
                   for line in p.read_text(encoding='utf-8').splitlines()]
        return [r for r in records if r['event'] == 'request']

    def response(self, content):
        return httpx.Response(200, request=httpx.Request('POST', 'https://api.deepseek.com/chat/completions'),
                              json={'choices': [{'message': {'content': content}}]})

    def test_executor_carries_metadata_and_resets_after_failure(self):
        seen = []
        def fail():
            seen.append(current_attempt())
            raise httpx.ReadTimeout('timeout')
        def success():
            seen.append(current_attempt())
        async def run():
            with self.assertRaises(httpx.ReadTimeout):
                await server._llm_call(fail, _attempt=2, _generation_id='logical-call')
            await server._llm_call(success)
        asyncio.run(run())
        self.assertEqual(seen[0], {'generation_id': 'logical-call', 'attempt': 2})
        self.assertEqual(seen[1]['attempt'], 1)
        self.assertNotEqual(seen[1]['generation_id'], 'logical-call')
        self.assertEqual(current_attempt(), {})

    def test_event_and_prose_json_retries_share_group_only_with_their_own_attempts(self):
        state = StoryState(genre='драма', tone='лёгкий', target_length=6, build_all_branches=False)
        state.add_character('Анна', 30)
        event = {'v': 3, 'participants': ['Анна'], 'ending': 'victory',
                 'happening': {'subject': 'Анна', 'verb': 'возвращается', 'object': 'домой'}}
        responses = [self.response('{"events": []}'), self.response(json.dumps({'events': [event]})),
                     self.response('{"replacements": []}'),
                     self.response('invalid json'),
                     self.response(json.dumps({'scenes': [{'narration': 'Анна вернулась домой.'}]}))]
        async def run():
            tree = {}
            root = await server._build_tree_events(state, self.client, tree, asyncio.Semaphore(1),
                                                   'test', None, 6, None, None)
            await server._materialize_segment('test', None, state, root, tree, self.client)
            self.assertEqual(tree[root]['scene']['narration'], 'Анна вернулась домой.')
        with patch('deepseek_client.httpx.Client') as factory, patch.object(server, '_log_debug'), patch.object(server, '_save_snapshot'):
            post = factory.return_value.__enter__.return_value.post
            post.side_effect = responses
            asyncio.run(run())
            self.assertEqual(post.call_count, 5)
            for call in post.call_args_list:
                for field in ('attempt', 'generation_id', '_attempt', '_generation_id'):
                    self.assertNotIn(field, call.kwargs['json'])
        records = self.requests()
        self.assertEqual([r['attempt'] for r in records], [1, 2, 1, 1, 2])
        self.assertEqual(records[0]['generation_id'], records[1]['generation_id'])
        self.assertEqual(records[3]['generation_id'], records[4]['generation_id'])
        self.assertNotEqual(records[0]['generation_id'], records[2]['generation_id'])
        self.assertEqual(len({r['request_id'] for r in records}), 5)

    def test_exhausted_event_retries_keep_limit_and_numbers(self):
        state = StoryState(genre='драма', tone='лёгкий', target_length=6)
        async def run():
            tree = {}
            root = await server._build_tree_events(state, self.client, tree, asyncio.Semaphore(1),
                                                   'test', None, 6, None, None)
            self.assertEqual(tree[root]['scene']['ending'], 'failure')
        with patch('deepseek_client.httpx.Client') as factory, patch.object(server, '_log_debug'), patch.object(server, '_save_snapshot'):
            post = factory.return_value.__enter__.return_value.post
            post.side_effect = httpx.ReadTimeout('timeout')
            asyncio.run(run())
            self.assertEqual(post.call_count, server.MAX_EVENT_GENERATION_ATTEMPTS)
        records = self.requests()
        self.assertEqual([r['attempt'] for r in records], list(range(1, len(records) + 1)))
        self.assertEqual(len({r['generation_id'] for r in records}), 1)

    def test_parallel_branches_have_independent_attempt_context(self):
        barrier = threading.Barrier(2)
        def send(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.response('{}')
        async def run():
            await asyncio.gather(*[
                server._llm_call(self.client._call, [{'role': 'user', 'content': name}], 0.2,
                                 _attempt=attempt, _generation_id=name)
                for name, attempt in [('branch-a', 2), ('branch-b', 3)]])
        with patch('deepseek_client.httpx.Client') as factory:
            factory.return_value.__enter__.return_value.post.side_effect = send
            asyncio.run(run())
        records = self.requests()
        self.assertEqual({r['generation_id']: r['attempt'] for r in records}, {'branch-a': 2, 'branch-b': 3})
        for record in records:
            self.assertEqual(record['generation_id'], record['payload']['messages'][0]['content'])

    def test_plan_retry_loop_passes_incrementing_metadata(self):
        seen = []
        client = Mock()
        def plan(*args):
            seen.append(current_attempt())
            if len(seen) == 1:
                raise httpx.ReadTimeout('timeout')
            return {}, {}
        client.generate_story_plan.side_effect = plan
        async def run():
            with patch.object(server, '_client', return_value=client), \
                 patch.object(server, '_remember_session'), patch.object(server, '_log_debug'), \
                 patch.object(server, '_build_tree_background', AsyncMock()), \
                 patch.dict(server.TREE_BUILD_TASKS, clear=True):
                result = await server.start_story(server.StartRequest(
                    seed='Завязка', genre='драма', tone='лёгкий', target_length=6))
                await server.TREE_BUILD_TASKS[result['session_id']]
        asyncio.run(run())
        self.assertEqual([r['attempt'] for r in seen], [1, 2])
        self.assertEqual(seen[0]['generation_id'], seen[1]['generation_id'])

    def test_cast_retry_loop_passes_incrementing_metadata(self):
        seen = []
        client = Mock()
        def characters(*args):
            seen.append(current_attempt())
            if len(seen) == 1:
                raise httpx.ReadTimeout('timeout')
            return [{'name': 'Анна', 'age': 30}], [], {}
        client.generate_characters.side_effect = characters
        async def tree(state, client, nodes, *args):
            nodes['root'] = {'scene': {'narration': 'Конец'}, 'children': {}}
            return 'root'
        state = StoryState(genre='драма', tone='лёгкий', target_length=6)
        req = server.StartRequest(seed='Завязка', genre='драма', tone='лёгкий', target_length=6)
        with patch.object(server, '_client', return_value=client), \
             patch.object(server, '_log_debug'), patch.object(server, '_save_snapshot'), \
             patch.object(server, '_build_tree_events', side_effect=tree), \
             patch.dict(server.TREE_BUILD_STATUS, clear=True):
            asyncio.run(server._build_tree_background('test-cast', state, req))
            self.assertIsNone(server.TREE_BUILD_STATUS['test-cast']['error'])
        self.assertEqual([r['attempt'] for r in seen], [1, 2])
        self.assertEqual(seen[0]['generation_id'], seen[1]['generation_id'])


if __name__ == '__main__':
    unittest.main()
