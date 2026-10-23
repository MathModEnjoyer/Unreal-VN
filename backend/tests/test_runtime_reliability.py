import copy
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_store
import httpx
import server
from deepseek_client import DeepSeekClient
from state_machine import StoryState
from storage import atomic_json


def current_state():
    state = StoryState(genre='драма', tone='лёгкий', target_length=30, build_all_branches=False)
    state.add_character('Анна', 30, is_protagonist=True)
    state.add_character('Борис', 35)
    state.tree_root_id = state.tree_current_id = 'root'
    state.story_tree = {
        'root': {'scene': {'narration': 'Начало', 'choices': [], 'participants': ['c2']},
                 'children': {'Продолжить': 'next'}, 'history_records': []},
        'next': {'scene': {'narration': 'Продолжение', 'choices': [], 'participants': ['c2']},
                 'children': {'Продолжить': 'end'}, 'history_records': []},
        'end': {'scene': {'narration': 'Конец', 'ending': 'complete'}, 'children': {}, 'history_records': []}}
    return state


class PersistenceReliabilityTests(unittest.TestCase):
    def test_failed_atomic_replace_preserves_previous_save_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'save.json'
            atomic_json(path, {'old': True})
            with patch('storage.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    atomic_json(path, {'old': False})
            self.assertEqual(json.loads(path.read_text()), {'old': True})
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['save.json'])

    def test_old_save_keeps_story_and_visual_identity_after_obsolete_fields_are_removed(self):
        snapshot = current_state().to_dict()
        expected_tree = StoryState.from_dict(snapshot).story_tree
        snapshot.update(model='deepseek-reasoner', violence_allowed=True, choices_given=42,
                        entity_issues=['obsolete'], unregistered_speakers=['obsolete'])
        snapshot['characters']['c2'].update(last_seen_scene=99, dormant=True)
        snapshot['event_history'] = [{'scene': 1, 'event': {'happening': {'verb': 'ждёт'},
                                                        'resources': [{'name': 'вода', 'amount': '2 л'}]}}]
        original = json.loads(json.dumps(snapshot))
        restored = StoryState.from_dict(snapshot)
        self.assertEqual(snapshot, original)
        self.assertEqual(restored.event_history, original['event_history'])
        self.assertEqual(restored.story_tree, expected_tree)
        self.assertEqual(restored.sprite_seed, original['sprite_seed'])
        self.assertEqual(restored.characters['c2'].name, original['characters']['c2']['name'])
        self.assertEqual(restored.characters['c2'].age, original['characters']['c2']['age'])
        for field in ('model', 'violence_allowed', 'choices_given', 'entity_issues', 'unregistered_speakers'):
            self.assertNotIn(field, restored.to_dict())
        self.assertNotIn('dormant', restored.to_dict()['characters']['c2'])

    def test_invalid_serialization_never_truncates_saved_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'save.json'
            atomic_json(path, {'old': True})
            with self.assertRaises(TypeError):
                atomic_json(path, {'invalid': object()})
            self.assertTrue(json.loads(path.read_text())['old'])

    def test_settings_empty_patch_keeps_key_and_explicit_empty_removes_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config_store, 'CONFIG_PATH', Path(directory) / 'config.json'):
            config_store.save_partial({'deepseek_api_key': 'test-only'})
            asyncio.run(server.update_settings(server.SettingsRequest()))
            self.assertTrue(config_store.status()['deepseek_api_key'])
            asyncio.run(server.update_settings(server.SettingsRequest(deepseek_api_key='')))
            self.assertFalse(config_store.status()['deepseek_api_key'])


class ModelFormatReliabilityTests(unittest.TestCase):
    def test_malformed_api_envelopes_raise_retryable_format_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = DeepSeekClient(api_key='test-only', log_dir=directory)
            # Isolate this suite from the separately tested review API.
            client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
            for payload in [[], {'choices': []}, {'choices': [None]}, {'choices': [{'message': {'content': None}}]}]:
                response = httpx.Response(200, request=httpx.Request('POST', 'https://api.deepseek.com/chat/completions'), json=payload)
                with self.subTest(payload=payload), patch('deepseek_client.httpx.Client') as factory:
                    factory.return_value.__enter__.return_value.post.return_value = response
                    with self.assertRaises(KeyError):
                        client._call([{'role':'user','content':'test'}], .3)

    def test_malformed_questions_cannot_crash_the_onboarding_screen(self):
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=json.dumps({'questions': [None, {'question':42},
            {'question':'Где?', 'options':None}, {'question':'Где?', 'options':['Дом',None,'Дом','Улица']}]}))
        self.assertEqual(client.generate_clarifying_questions('test','драма','лёгкий'),
                         [{'question':'Где?','options':['Дом','Улица']}])

    def test_wrong_types_in_optional_dialogue_do_not_abort_paid_prose(self):
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value=json.dumps({'scenes': [
            {'narration':'Текст', 'dialogue':{'bad':'shape'}, 'speaker_id':[]}, {'narration':'  '}]}))
        scenes, _ = client.generate_segment_prose(current_state(), ['Событие'], None)
        self.assertEqual(scenes, [{'narration':'Текст'}])

    def test_null_genre_lists_leave_available_options_intact(self):
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(return_value='{"genres":null,"tones":false}')
        self.assertEqual(client.generate_genre_tone_fit('test',['a','b','c'],['x','y','z']),
                         (['a','b','c'],['x','y','z']))


class NavigationReliabilityTests(unittest.TestCase):
    def test_restoring_unseen_first_scene_does_not_repeat_it_on_continue(self):
        state = current_state()
        with (patch.object(server, '_get_state', return_value=state),
              patch.object(server, '_save_snapshot'), patch.object(server, '_append_transcript'),
              patch.object(server, '_scene_with_visuals', side_effect=lambda s, scene, **kw: scene)):
            async def run():
                await server._peek_session_impl('test')
                response = await server._next_scene_impl(server.SceneRequest(session_id='test', choice='Продолжить'))
                self.assertEqual(response['scene']['narration'], 'Продолжение')
                self.assertEqual(state.delivered_scenes, 2)
            asyncio.run(run())

    def test_two_queued_clicks_from_one_scene_do_not_skip_a_scene(self):
        state = current_state()
        state.tree_first_delivered = True
        with (patch.object(server, '_get_state', return_value=state),
              patch.object(server, '_save_snapshot'), patch.object(server, '_append_transcript'),
              patch.object(server, '_scene_with_visuals', side_effect=lambda s, scene, **kw: scene),
              patch.dict(server.SESSION_LOCKS, clear=True)):
            async def run():
                req = server.SceneRequest(session_id='test', choice='Продолжить', from_node_id='root')
                responses = await asyncio.gather(server.next_scene(req), server.next_scene(req), return_exceptions=True)
                self.assertEqual(sum(isinstance(r, server.HTTPException) for r in responses), 1)
                self.assertEqual(state.tree_current_id, 'next')
                failure = next(r for r in responses if isinstance(r, server.HTTPException))
                self.assertEqual(failure.detail['reason'], 'stale_scene')
            asyncio.run(run())

    def test_lock_cleanup_never_evicts_an_in_use_lock(self):
        async def run():
            with patch.dict(server.SESSION_LOCKS, clear=True):
                first = server._session_lock('busy')
                async with first:
                    for i in range(160):
                        server._session_lock(f'id-{i}')
                    self.assertIs(server._session_lock('busy'), first)
        asyncio.run(run())

    def test_deleting_build_cancels_task_and_does_not_recreate_save(self):
        async def run(directory):
            async def build():
                await asyncio.Event().wait()
            task = asyncio.create_task(build())
            await asyncio.sleep(0)
            server.TREE_BUILD_TASKS['test'] = task
            server.TREE_BUILD_STATUS['test'] = {'done': False}
            server.SESSIONS['test'] = current_state()
            await server.drop_session('test')
            self.assertTrue(task.cancelled())
            self.assertNotIn('test', server.SESSIONS)
            self.assertNotIn('test', server.TREE_BUILD_STATUS)
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(server, 'SNAPSHOT_DIR', Path(directory)),
              patch.dict(server.SESSIONS, clear=True), patch.dict(server.TREE_BUILD_TASKS, clear=True),
              patch.dict(server.TREE_BUILD_STATUS, clear=True), patch.dict(server.SESSION_LOCKS, clear=True)):
            asyncio.run(run(directory))

    def test_prose_failure_keeps_paid_events_and_can_be_retried(self):
        state = current_state()
        state.story_tree['root'] = {'scene': None, 'segment_events': ['Событие'],
                                    'segment_choice_texts': None, 'segment_children_ids': {}}
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(side_effect=[httpx.ReadTimeout('offline')] * 3 +
                                       [json.dumps({'scenes': [{'narration': 'Готово'}]})])
        with (patch.object(server, '_client', return_value=client), patch.object(server, '_log_debug'),
              patch.object(server, '_save_snapshot')):
            async def run():
                with self.assertRaises(server.HTTPException) as failure:
                    await server._ensure_materialized('test', state, 'root')
                self.assertEqual(failure.exception.status_code, 502)
                self.assertIsNone(state.story_tree['root']['scene'])
                node = await server._ensure_materialized('test', state, 'root')
                self.assertEqual(node['scene']['narration'], 'Готово')
                self.assertEqual(client._call.call_count, 4)
            asyncio.run(run())

    def test_custom_action_failure_keeps_pivot_and_reuses_paid_plan(self):
        state = current_state()
        state.build_all_branches = True
        state.tree_first_delivered = True
        state.story_tree['root']['children'] = {'A': 'next', 'B': 'end'}
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(side_effect=[json.dumps({'accepted': True, 'resulting_events': ['Уйти']}),
                                       *([httpx.ReadTimeout('offline')] * 3),
                                       json.dumps({'scenes': [{'narration': 'Готово'}]})])
        with (patch.object(server, '_get_state', return_value=state),
              patch.object(server, '_client', return_value=client), patch.object(server, '_log_debug'),
              patch.object(server, '_save_snapshot') as save, patch.object(server, '_append_transcript'),
              patch.object(server, '_scene_with_visuals', side_effect=lambda s, scene, **kw: scene)):
            async def run():
                req = server.CustomChoiceRequest(session_id='test', custom_text='Уйти', from_node_id='root')
                with self.assertRaises(server.HTTPException):
                    await server._custom_choice_impl(req)
                self.assertEqual(state.tree_current_id, 'root')
                self.assertEqual(state.story_tree[state.story_tree['root']['children']['Уйти']]['segment_events'], ['Уйти'])
                response = await server._custom_choice_impl(req)
                self.assertTrue(response['accepted'])
                self.assertEqual(client._call.call_count, 5)
                self.assertTrue(save.called)
                self.assertEqual(state.delivered_scenes, 1)
                # Revisiting the pivot and entering the same action reuses prose too.
                state.tree_current_id = 'root'
                await server._custom_choice_impl(req)
                self.assertEqual(client._call.call_count, 5)
            asyncio.run(run())

    def test_pending_event_failure_does_not_replace_branch_with_permanent_ending(self):
        state = current_state()
        pending = server._make_pending_branch(state.story_tree, state, 'Пойти', 20, (16,20))
        before = state.story_tree[pending].copy()
        client = DeepSeekClient(api_key='test-only')
        # Isolate this suite from the separately tested review API.
        client.review_segment_events = Mock(side_effect=lambda state, events, **kw: (copy.deepcopy(events), [], {}))
        client._call = Mock(side_effect=httpx.ReadTimeout('offline'))
        with (patch.object(server, '_client', return_value=client), patch.object(server, '_log_debug'),
              patch.object(server, '_save_snapshot')):
            with self.assertRaises(server.HTTPException) as failure:
                asyncio.run(server._build_pending_branch('test', state, pending))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(state.story_tree[pending], before)
        self.assertEqual(client._call.call_count, server.MAX_EVENT_GENERATION_ATTEMPTS)

    def test_resuming_at_point_persists_cursor_and_resets_visible_progress(self):
        state = current_state()
        state.delivered_scenes = 99
        with (patch.object(server, '_get_state', return_value=state), patch.object(server, '_save_snapshot') as save,
              patch.object(server, '_scene_with_visuals', side_effect=lambda s, scene, **kw: scene)):
            response = asyncio.run(server._resume_at_impl(server.ResumeRequest(session_id='test', node_id='next')))
        self.assertEqual(response['state']['current_scene'], state.current_scene)
        self.assertEqual(state.tree_current_id, 'next')
        save.assert_called_once()
