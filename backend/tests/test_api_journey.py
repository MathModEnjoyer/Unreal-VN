from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import config_store
import server
from prompts import choice_windows
from scenario_client import ScenarioClient


class APIJourneyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.directory.joinpath('sessions').mkdir()
        patches = [patch.object(config_store, 'CONFIG_PATH', self.directory / 'config.json'),
                   patch.object(server, 'SAVE_DIR', self.directory),
                   patch.object(server, 'SNAPSHOT_DIR', self.directory / 'sessions'),
                   patch.object(server, '_client', side_effect=lambda *a, **kw: ScenarioClient(**kw)),
                   patch.dict(server.SESSIONS, clear=True), patch.dict(server.SESSION_LOCKS, clear=True),
                   patch.dict(server.TREE_BUILD_TASKS, clear=True), patch.dict(server.TREE_BUILD_STATUS, clear=True)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        ScenarioClient.calls = []
        self.api = TestClient(server.app)
        self.api.__enter__()
        self.addCleanup(self.api.__exit__, None, None, None)

    def start(self, full, length=30):
        response = self.api.post('/story/start', json={'seed': 'Исчезновение', 'genre': 'детектив',
                                 'tone': 'мрачный', 'target_length': length, 'build_all_branches': full})
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()['session_id']
        for _ in range(300):
            status = self.api.get(f'/story/tree_status/{sid}')
            if status.status_code == 200 and status.json()['done']:
                self.assertFalse(status.json().get('error'), status.text)
                return sid
            time.sleep(.01)
        self.fail('Fixture tree did not finish')

    def next(self, sid, choice='Продолжить', node=None):
        data = {'session_id': sid, 'choice': choice}
        if node:
            data['from_node_id'] = node
        response = self.api.post('/story/scene', json=data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_complete_economy_and_normal_journeys_with_restart_and_no_paid_replay(self):
        for full, length in [(False, 30), (True, 30), (False, 95), (True, 95)]:
            with self.subTest(full=full, length=length):
                ScenarioClient.calls.clear()
                sid = self.start(full, length)
                first = self.next(sid)
                self.assertEqual(len(first['scene']['characters']), 2)
                self.assertEqual({c['id'] for c in first['scene']['characters']}, {'c2', 'c3'})
                state = server._get_state(sid)
                self.assertEqual(state.characters['c1'].age, 18)
                self.assertEqual(state.characters['c1'].name, state.characters['c2'].name)
                pivots = []
                response = first
                read = 1
                while not response['finished']:
                    choices = response['scene'].get('choices') or []
                    node_id = response['state']['node_id']
                    if choices:
                        self.assertEqual(len(choices), 2)
                        pivots.append(node_id)
                    response = self.next(sid, choices[-1] if choices else 'Продолжить', node_id)
                    read += 1
                    self.assertLess(read, 180)
                expected_segments = len(choice_windows(length)) + 1
                self.assertEqual(len(pivots), expected_segments - 1)
                self.assertEqual(response['state']['current_scene'], read)
                self.assertEqual(response['scene']['ending'], 'victory')
                self.assertEqual(ScenarioClient.calls.count('generate_segment_prose'), expected_segments)
                self.assertEqual(ScenarioClient.calls.count('review_segment_events'), expected_segments)
                self.assertEqual(ScenarioClient.calls.count('generate_segment_events'),
                                 1 + (expected_segments - 1) * (2 if full else 1))
                self.assertEqual(response['state']['usage']['cache_hit_tokens'], response['state']['usage']['api_calls'] * 80)

                # Simulate losing all process memory: only the disk snapshot remains.
                server.SESSIONS.pop(sid)
                count = len(ScenarioClient.calls)
                restored = self.api.get(f'/story/session/{sid}')
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual(restored.json()['state']['node_id'], response['state']['node_id'])
                self.assertEqual(restored.json()['state']['current_scene'], read)
                self.assertEqual(len(ScenarioClient.calls), count)

                replay = self.api.post('/story/resume_at', json={'session_id': sid, 'node_id': pivots[0]})
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(len(ScenarioClient.calls), count)
                # Reading the already played side again must reuse all prose.
                response = self.next(sid, replay.json()['scene']['choices'][-1], pivots[0])
                self.assertEqual(len(ScenarioClient.calls), count)
                self.assertTrue(response['scene']['narration'])

                exported = self.api.get(f'/story/export/{sid}.md')
                self.assertEqual(exported.status_code, 200)
                self.assertIn('Продолжим поиски', exported.text)
                self.assertIn('attachment;', exported.headers['content-disposition'])

                # Actual generated diagnostics restore the played branch without API.
                log = (self.directory / f'{sid}_debug.jsonl').read_bytes()
                loaded = self.api.post('/story/load_jsonl', files={'file': ('saved.jsonl', log, 'application/octet-stream')})
                self.assertEqual(loaded.status_code, 200, loaded.text)
                self.assertTrue(loaded.json()['resume_points'])
                self.assertEqual(len(ScenarioClient.calls), count)

    def test_custom_action_rejection_confirmation_and_stale_proposal(self):
        sid = self.start(True)
        response = self.next(sid)
        while not response['scene']['choices']:
            response = self.next(sid)
        pivot = response['state']['node_id']
        rejection = self.api.post('/story/custom_choice', json={
            'session_id': sid, 'custom_text': 'Прыгнуть на Луну', 'from_node_id': pivot})
        self.assertEqual(rejection.status_code, 200, rejection.text)
        proposal = rejection.json()
        self.assertFalse(proposal['accepted'])
        self.assertEqual(len(proposal['clarifying_options']), 3)
        count = len(ScenarioClient.calls)
        accepted = self.api.post('/story/custom_choice', json={
            'session_id': sid, 'custom_text': proposal['clarifying_options'][0],
            'proposal_id': proposal['proposal_id'], 'from_node_id': pivot})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()['accepted'])
        self.assertEqual(len(ScenarioClient.calls), count + 3)
        self.assertNotEqual(accepted.json()['state']['node_id'], pivot)
        persisted = server.StoryState.load(str(server._snapshot_path(sid)))
        self.assertEqual(persisted.tree_current_id, accepted.json()['state']['node_id'])
        self.assertEqual(persisted.delivered_scenes, accepted.json()['state']['current_scene'])
        stale = self.api.post('/story/custom_choice', json={'session_id': sid, 'custom_text': 'Уйти',
                                                          'proposal_id': proposal['proposal_id'], 'from_node_id': pivot})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(ScenarioClient.calls), count + 3)

    def test_invalid_jsonl_variants_are_422_and_bom_is_accepted(self):
        for value in [b'null', b'[]', b'42', b'false', b'{broken', b'\xff', b'',
                      b'{"step":"plan","raw_plan":true}\n{"step":"characters","raw_characters":42}']:
            with self.subTest(value=value):
                response = self.api.post('/story/load_jsonl', files={'file': ('broken.jsonl', value)})
                self.assertEqual(response.status_code, 422, response.text)
        sid = self.start(False)
        self.next(sid)
        log = (self.directory / f'{sid}_debug.jsonl').read_bytes()
        response = self.api.post('/story/load_jsonl', files={'file': ('bom.jsonl', b'\xef\xbb\xbf' + log)})
        self.assertEqual(response.status_code, 200, response.text)

    def test_runtime_assets_are_available_and_disallowed_paths_are_rejected(self):
        self.assertEqual(self.api.get('/').status_code, 200)
        for path in ['/health', '/settings', '/music-credits', '/ambience/rain.ogg', '/ambience/storm.ogg',
                     '/backgrounds/abstract_space__day.webp']:
            self.assertEqual(self.api.get(path).status_code, 200, path)
        self.assertEqual(self.api.get('/characters/not-an-asset.png').status_code, 404)
        self.assertEqual(self.api.get('/music/unknown.mp3').status_code, 404)
        self.assertEqual(self.api.get('/story/unknown-endpoint').status_code, 404)
        self.assertEqual(self.api.post('/story/scene', json={'session_id': '..', 'choice': 'Продолжить'}).status_code, 400)

    def test_other_websites_cannot_read_settings_or_trigger_paid_generation(self):
        for path in ['/settings', '/story/premises']:
            response = self.api.get(path, headers={'Origin':'https://unrelated.example'})
            self.assertEqual(response.status_code, 403)
        response = self.api.get('/story/premises', headers={'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ScenarioClient.calls, [])
        for origin in ['http://localhost:1420', 'http://127.0.0.1:8756', 'http://testserver']:
            self.assertEqual(self.api.get('/settings', headers={'Origin':origin}).status_code, 200)
