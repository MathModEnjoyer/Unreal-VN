import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state_machine import StoryState
from character_identity import resolve_ref
from deepseek_client import DeepSeekClient
from story_events import normalize_event, bind_event
from prompts import build_character_planning_prompt, build_segment_prose_prompt
import server


def state():
    result = StoryState(genre='драма', tone='лёгкий', target_length=8)
    result.add_character('Анна', 24, is_protagonist=True)
    result.add_character('Анна', 34)
    return result


class CharacterIdentityTests(unittest.TestCase):
    def test_same_name_characters_keep_separate_identity_through_json_and_branches(self):
        current = state()
        self.assertEqual(list(current.characters), ['c1', 'c2'])
        self.assertIsNone(current.character_id('Анна'))
        self.assertIsNone(resolve_ref(current.characters, 'Анна (по рации)', aliases=True))
        restored = StoryState.from_dict(json.loads(json.dumps(current.to_dict())))
        restored.touch_character('c2')
        self.assertTrue(restored.characters['c2'].revealed)
        self.assertFalse(current.characters['c2'].revealed)
        self.assertEqual(restored.characters['c1'].age, 24)
        self.assertEqual(restored.characters['c2'].age, 34)
        with self.assertRaises(ValueError):
            restored.add_character('Борис', 30, character_id='c2')
        self.assertEqual(len(restored.characters), 2)

    def test_duplicate_model_ids_are_repaired_without_overwrite_or_extra_call(self):
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'characters': [
            {'id':'c4','name':'Анна','age':24}, {'id':'c4','name':'Анна','age':34},
            {'id':'c1','name':'Борис','age':44}],
            'relationships':[{'between':['c4','c1'],'type':'коллеги','attitude':'доверие'}]}))
        chars, rels, _ = client.generate_characters('seed','драма','лёгкий',{},2,3)
        self.assertEqual(len({c['id'] for c in chars}), 3)
        self.assertEqual(chars[2]['id'], 'c1')
        self.assertEqual(rels, [])  # The duplicated c4 had no unique target.
        client._call.assert_called_once()

    def test_ids_own_participants_actions_holder_and_speaker_not_the_display_label(self):
        current = state()
        source = {'v':3,'participants':['c2'],
                  'happening':{'subject':'c2','verb':'передаёт','object':'c1'},
                  'actions':[{'actor':'c1','verb':'принимает','object':'письмо'}],
                  'entities':[{'name':'письмо','holder':'c1'}]}
        event = normalize_event(source, current.characters)
        bound = bind_event({'narration':'Письмо передано.', 'speaker_id':'c2',
                            'speaker':'Неверное имя','dialogue':'Держи.'}, event)
        fields = DeepSeekClient.apply_scene_to_state(current, bound)
        self.assertEqual((fields['speaker_id'], fields['speaker']), ('c2','Анна'))
        self.assertEqual(current.recent_lines[-1]['speaker_id'], 'c2')
        self.assertTrue(current.characters['c2'].revealed)
        self.assertFalse(current.characters['c1'].revealed)
        self.assertEqual(current.event_history[-1]['event'], event)
        self.assertEqual(current.event_history[-1]['event']['entities'][0]['holder'], 'c1')

    def test_unknown_id_never_falls_back_to_a_name_or_discards_dialogue(self):
        current = StoryState(genre='драма', tone='лёгкий', target_length=8)
        current.add_character('Анна', 24)
        for raw in [{'speaker_id':'c999','speaker':'Анна'}, {'speaker_id':'c999'},
                    {'speaker_id':[], 'speaker':'Анна'}, {'speaker_id':None,'speaker':'Анна'}]:
            fields = DeepSeekClient.apply_scene_to_state(current, {
                'narration':'Слышен голос.', 'dialogue':'Кто здесь?', **raw})
            self.assertIsNone(fields['speaker_id'])
            self.assertTrue(fields['speaker'])
            self.assertIsNone(current.recent_lines[-1]['speaker_id'])
            self.assertFalse(current.characters['c1'].revealed)
        self.assertEqual(len(current.characters), 1)

    def test_legacy_alias_matching_is_unique_and_ambiguous_names_are_not_guessed(self):
        current = StoryState(genre='драма', tone='лёгкий', target_length=8)
        current.add_character('Елена Волкова', 30)
        current.add_character('Ирина Волкова', 40)
        self.assertIsNone(resolve_ref(current.characters, 'Волкова', aliases=True))
        self.assertEqual(resolve_ref(current.characters, 'Елена Волкова (по рации)', aliases=True), 'c1')
        fields = DeepSeekClient.apply_scene_to_state(current, {
            'narration':'Ответ.', 'speaker':'Елена Волкова (по рации)','dialogue':'Да.'})
        self.assertEqual(fields['speaker_id'], 'c1')
        self.assertEqual(fields['speaker'], 'Елена Волкова')

    def test_old_save_migrates_tree_and_deferred_state_without_rewriting_source_history(self):
        original = StoryState(genre='драма', tone='лёгкий', target_length=8)
        original.add_character('Анна', 24)
        original.add_character('Борис', 34)
        old = original.to_dict()
        old['characters'] = {c['name']:{k:v for k,v in c.items() if k!='id'}
                             for c in old['characters'].values()}
        old['character_relationships'] = [{'between':['Анна','Борис'],'type':'коллеги','attitude':'доверие'}]
        old['event_history'] = [{'event':{'participants':['Анна'],
            'happening':{'subject':'Анна','verb':'зовёт','object':'Бориса'}}}]
        old['recent_lines'] = [{'speaker':'Анна','text':'Борис!'}]
        nested = copy.deepcopy(old)
        old['story_tree'] = {'a':{'scene':{'narration':'Встреча.', 'speaker':'Борис','dialogue':'Да.'}},
                             'b':{'pending_state':nested}}
        before = copy.deepcopy(old)
        restored = StoryState.from_dict(old)
        self.assertEqual(old, before)
        self.assertEqual(restored.event_history, before['event_history'])
        self.assertEqual(restored.character_relationships[0]['between'], ['c1','c2'])
        self.assertEqual(restored.recent_lines[0]['speaker_id'], 'c1')
        self.assertEqual(restored.story_tree['a']['scene']['speaker_id'], 'c2')
        branch = StoryState.from_dict(restored.story_tree['b']['pending_state'])
        self.assertEqual(list(branch.characters), ['c1','c2'])
        self.assertEqual(StoryState.from_dict(restored.to_dict()).to_dict(), restored.to_dict())

    def test_public_notebook_and_relationships_distinguish_identical_names(self):
        current = state()
        current.character_relationships = [{'between':['c1','c2'],'type':'коллеги','attitude':'доверие'}]
        current.touch_character('c2')
        public = server._public_state(current)
        self.assertEqual([c['id'] for c in public['characters']], ['c1','c2'])
        self.assertEqual(public['characters'][1]['relation_type'], 'коллеги')
        self.assertIn('c1: Анна', current.characters_block())
        self.assertIn('c2: Анна', current.characters_block())

    def test_materialization_and_jsonl_replay_keep_the_same_speaker_ids(self):
        current = state()
        event = {'v':3,'participants':['c2'],'happening':{'subject':'c2','verb':'отвечает','object':'на вопрос'}}
        current.story_tree = {'root':{'segment_events':[event], 'segment_choice_texts':None,
                                     'segment_children_ids':{},'origin_choice':None}}
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'scenes':[{
            'narration':'Собеседница ответила.', 'speaker_id':'c2','dialogue':'Хорошо.'}]}))
        entries = [{'step':'plan','raw_plan':{},'genre':'драма','tone':'лёгкий','target_length':8},
                   {'step':'characters','raw_characters':[vars(c) for c in current.characters.values()]},
                   {'step':'segment_events','events':[event],'ceiling_scene':8,'window':None,
                    'origin_choice':None,'raw_pivot_choices':None}]
        def log(session,path,step,payload):entries.append({'step':step,**copy.deepcopy(payload)})
        with patch.object(server,'_log_debug',side_effect=log):
            asyncio.run(server._materialize_segment('test',None,current,'root',current.story_tree,client))
        scene=current.story_tree['root']['scene']
        self.assertEqual((scene['speaker_id'],scene['speaker']), ('c2','Анна'))
        restored,_=server._reconstruct_session(entries)
        replayed=restored.story_tree[restored.tree_root_id]['scene']
        self.assertEqual((replayed['speaker_id'],replayed['speaker']), ('c2','Анна'))
        self.assertEqual(list(restored.characters), ['c1','c2'])
        client._call.assert_called_once()

    def test_generation_contracts_request_ids_in_the_existing_calls(self):
        cast=build_character_planning_prompt('seed','драма','лёгкий',{},2,3)
        self.assertIn('"id": "c1"',cast)
        self.assertIn('"between": ["c1", "c2"]',cast)
        prose=build_segment_prose_prompt([],None)
        self.assertIn('speaker_id',prose)
        self.assertNotIn('добавь speaker и dialogue',prose)


if __name__ == '__main__':
    unittest.main()
