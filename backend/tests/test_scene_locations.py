import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from state_machine import StoryState
from scene_locations import apply_scene_location, load_places, restore_location_path
from visual_retrieval import VisualCatalog
from prompts import build_segment_prose_prompt


def scene(name='Спальня Анны', place='anna_room', narration='Мы сидели у окна.', kind=None):
    return {'location':name, 'location_id':place, 'narration':narration,
            'location_background_id': {'anna_room':'bedroom','boris_room':'bedroom','harbor':'harbor_quay','captain_cabin':'ship_cabin','mine':'mine','mine_entrance':'mine'}.get(place),
            'location_transition':{'kind':kind, 'evidence':narration} if kind else None}


class SceneLocationTests(unittest.TestCase):
    def state(self):
        return StoryState(genre='драма', tone='лёгкий', target_length=30)

    def test_stay_preserves_id_name_and_background_despite_other_mentions(self):
        state = self.state()
        first = DeepSeekClient.apply_scene_to_state(state, scene())
        self.assertEqual(first['location_background_id'], 'bedroom')
        variation = scene('Небольшая комната', narration='Мы обсуждали порт, лес и кузницу.')
        variation['time_of_day'] = 'night'
        variation['atmosphere'] = {'weather':'rain'}
        result = DeepSeekClient.apply_scene_to_state(state, variation)
        self.assertEqual(result['location'], 'Спальня Анны')
        self.assertEqual(result['location_id'], first['location_id'])
        self.assertEqual(result['location_background_id'], first['location_background_id'])
        self.assertEqual(state.scene_conditions['atmosphere']['weather'], 'rain')

    def test_move_cut_and_return_restore_original_background(self):
        state = self.state()
        first = apply_scene_location(state, scene())
        harbor = apply_scene_location(state, scene('Порт, причал', 'harbor', 'Мы вышли на портовый причал.', 'move'))
        self.assertEqual(harbor['location_id'], 'harbor')
        self.assertEqual(harbor['location_background_id'], 'harbor_quay')
        cabin = apply_scene_location(state, scene('Каюта капитана', 'captain_cabin', 'Позже, уже в каюте капитана, мы открыли карту.', 'cut'))
        self.assertEqual(cabin['location_id'], 'captain_cabin')
        result = apply_scene_location(state, scene('Наша старая комната', narration='Мы вернулись в спальню Анны.', kind='move'))
        self.assertEqual(result['location_id'], first['location_id'])
        self.assertEqual(result['location'], first['location'])
        self.assertEqual(result['location_background_id'], first['location_background_id'])
        self.assertEqual(len(state.locations), 3)

    def test_plans_dialogue_choices_and_missing_proof_do_not_move(self):
        for metadata in [None, {'kind':'stay'}, {'kind':'move','evidence':'Мы вышли на причал.'},
                         {'kind':'teleport','evidence':'Мы вышли на причал.'}, [], 'move']:
            state = self.state()
            apply_scene_location(state, scene())
            proposed = scene('Порт', 'harbor', 'Мы обсуждали дорогу к морю.')
            proposed.update(location_transition=metadata, dialogue='Мы вышли на причал.', choices=['Выйти в порт'])
            result = apply_scene_location(state, proposed)
            self.assertEqual(result['location_id'], 'anna_room')
            self.assertEqual(result['location_issue'], 'location_transition_required')
            self.assertNotIn('harbor',state.locations)
        # No structured transition means prose alone cannot change the place.
        state = self.state(); apply_scene_location(state, scene())
        self.assertEqual(apply_scene_location(state, scene('Порт','harbor','Мы пришли в порт.'))['location_id'],'anna_room')

    def test_similar_rooms_are_distinct_and_exact_names_reuse_identity(self):
        state = self.state(); apply_scene_location(state, scene())
        other = apply_scene_location(state, scene('Спальня Бориса','boris_room','Мы вошли в спальню Бориса.','move'))
        self.assertEqual(other['location_id'],'boris_room')
        self.assertEqual(len(state.locations),2)
        result = apply_scene_location(state, scene('Спальня Анны','new_room_name','Мы вернулись в спальню Анны.','move'))
        self.assertEqual(result['location_id'],'anna_room')
        self.assertEqual(len(state.locations),2)

    def test_registry_survives_save_and_branches_do_not_share_places(self):
        state = self.state(); apply_scene_location(state, scene())
        restored = StoryState.from_dict(json.loads(json.dumps(state.to_dict())))
        branch = copy.deepcopy(restored)
        apply_scene_location(branch, scene('Шахта','mine_entrance','Мы спустились в шахту.','move'))
        self.assertEqual(restored.location_id, 'anna_room')
        self.assertNotIn('mine_entrance', restored.locations)
        self.assertEqual(branch.location_id,'mine_entrance')
        restored.locations['anna_room']['name']='Изменено в копии'
        self.assertEqual(state.locations['anna_room']['name'],'Спальня Анны')
        self.assertNotIn('anna_room', state.to_prompt_block())  # local registry is not repeated

    def test_legacy_saves_and_explicit_legacy_replay(self):
        original=self.state(); original.location='Спальня Анны'
        data=original.to_dict(); data.pop('location_id'); data.pop('locations')
        restored=StoryState.from_dict(data)
        self.assertTrue(restored.location_id.startswith('place_'))
        self.assertEqual(restored.locations[restored.location_id]['name'],'Спальня Анны')
        moved=DeepSeekClient.apply_scene_to_state(restored,{'location':'Шахта','narration':'Темно.'},legacy_locations=True)
        self.assertEqual(moved['location'],'Шахта')
        held=DeepSeekClient.apply_scene_to_state(restored,{'location':'Порт','narration':'Думаем о море.'})
        self.assertEqual(held['location'],'Шахта')

    def test_malformed_metadata_cannot_crash_or_inject_backgrounds(self):
        for invalid in [[], {}, 1, True, '../file', 'UPPER', 'a'*65]:
            state=self.state(); apply_scene_location(state,scene())
            result=apply_scene_location(state,{'location_id':invalid,'location':['Порт'],
                'location_transition':{'kind':[],'evidence':{}},'narration':{},'location_background_id':'mine'})
            self.assertEqual(result['location_id'],'anna_room')
            self.assertEqual(result['location_background_id'],'bedroom')
        self.assertEqual(load_places({'bad':[],'../bad':{'name':'x'},'room':{'name':[]}}),{})

    def test_missing_selection_and_removed_id_use_fallback_without_guessing(self):
        state=self.state()
        missing=apply_scene_location(state,scene('Неведомая фырбукальная штука','odd_place'))
        self.assertIsNone(missing['location_background_id'])
        catalog=VisualCatalog()
        result=catalog.recommend('Спальня',background_id='removed_asset')
        self.assertEqual(result['backgrounds'][0]['asset_id'],'abstract_space')
        self.assertEqual(result['fallback']['reason'],'invalid_background_id')

    def test_generation_adds_metadata_in_its_only_call(self):
        state=self.state(); DeepSeekClient.apply_scene_to_state(state, scene())
        client=DeepSeekClient(api_key='test-key'); client._last_usage={}
        raw=scene()
        with patch.object(client,'_call',return_value=json.dumps({'scenes':[raw]})) as call:
            scenes,_=client.generate_segment_prose(state,['Разговор у окна'],None)
        call.assert_called_once()
        self.assertEqual(scenes[0]['location_id'],'anna_room')
        prompt = build_segment_prose_prompt(['Разговор'],None)
        self.assertIn('location_transition', prompt)
        self.assertIn('воспоминания', prompt)
        self.assertNotIn('anna_room', prompt)



class LocationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home=tempfile.TemporaryDirectory()
        with patch('pathlib.Path.home',return_value=Path(cls.home.name)):
            import server
        cls.server=server

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def test_pinned_background_keeps_new_time_and_location_effect_rules(self):
        state=StoryState(genre='драма',tone='лёгкий',target_length=20)
        fields=DeepSeekClient.apply_scene_to_state(state,scene())
        fields.update(narration='В порту началась гроза.',time_of_day='night',atmosphere={'weather':'storm'})
        result=self.server._scene_with_visuals(state,fields)
        self.assertEqual(result['visual']['backgrounds'][0]['asset_id'],'bedroom')
        self.assertEqual(result['visual']['backgrounds'][0]['time_of_day'],'night')
        self.assertIsNone(result['visual']['effects']['weather'])
        self.assertEqual(result['visual']['effects']['grading']['strength'],.22)
        mine=DeepSeekClient.apply_scene_to_state(state,scene('Шахта','mine','Мы спустились в шахту.','move'))
        result=self.server._scene_with_visuals(state,{**mine,'time_of_day':'night','atmosphere':{'weather':'storm'}})
        self.assertEqual(result['visual']['backgrounds'][0]['time_mode'],'fixed')
        self.assertIsNone(result['visual']['effects']['grading'])


    def test_resume_another_branch_uses_only_its_ancestor_places(self):
        state=StoryState(genre='драма',tone='лёгкий',target_length=20)
        root=DeepSeekClient.apply_scene_to_state(state,scene())
        mine=DeepSeekClient.apply_scene_to_state(state,scene('Шахта','mine','Мы спустились в шахту.','move'))
        state.tree_root_id='root'
        tree={'root':{'scene':root,'children':{'A':'mine','B':'other'}},
              'mine':{'scene':mine,'children':{}},
              'other':{'scene':None,'segment_children_ids':{},'children':{}}}
        restore_location_path(state,tree,'other')
        self.assertEqual(state.location_id,'anna_room')
        self.assertEqual(set(state.locations),{'anna_room'})
        self.assertEqual(state.locations['anna_room']['background_id'],'bedroom')

    def test_old_jsonl_replays_moves_and_new_jsonl_keeps_strict_contract(self):
        first=scene()
        unconfirmed={'location':'Порт','narration':'Мы обсуждали порт.'}
        def entries(modern):
            events=['Разговор','Планы']
            prose={'step':'segment_prose','events':events,'raw_scenes':[first,unconfirmed]}
            if modern: prose['location_schema']=1
            return [{'step':'plan','genre':'драма','tone':'лёгкий','raw_plan':{}},
                    {'step':'characters','raw_characters':[]},
                    {'step':'segment_events','events':events,'ceiling_scene':2,'window':None},prose]
        with patch.object(DeepSeekClient,'_call',side_effect=AssertionError('No model calls during replay')):
            old,_=self.server._reconstruct_session(entries(False))
            new,_=self.server._reconstruct_session(entries(True))
        old_first=old.story_tree[old.tree_root_id]
        new_first=new.story_tree[new.tree_root_id]
        old_second=old.story_tree[next(iter(old_first['children'].values()))]['scene']
        new_second=new.story_tree[next(iter(new_first['children'].values()))]['scene']
        self.assertEqual(old_second['location'],'Порт')
        self.assertEqual(new_second['location_id'],'anna_room')
        self.assertEqual(new_second['location_background_id'],'bedroom')

    def test_materialized_nodes_use_accepted_location_fields(self):
        state=StoryState(genre='драма',tone='лёгкий',target_length=20)
        scenes=[scene(),scene('Порт','harbor','Мы обсуждали порт.'),
                scene('Шахта','mine','Мы спустились в шахту.','move')]
        tree={'root':{'segment_events':['Комната','Планы','Спуск'],
                      'segment_choice_texts':None,'segment_children_ids':{},'origin_choice':None}}
        state.story_tree=tree
        class Client:
            apply_scene_to_state=staticmethod(DeepSeekClient.apply_scene_to_state)
            def generate_segment_prose(self,*args): return scenes,{}
        with patch.object(self.server,'_log_debug') as log:
            asyncio.run(self.server._materialize_segment('test',None,state,'root',tree,Client()))
        root=tree['root']; second=tree[next(iter(root['children'].values()))]
        third=tree[next(iter(second['children'].values()))]
        self.assertEqual(second['scene']['location_id'],'anna_room')
        self.assertEqual(second['scene']['location'],'Спальня Анны')
        self.assertEqual(third['scene']['location_id'],'mine')
        self.assertEqual(third['scene']['location_background_id'],'mine')
        prose=next(call.args[3] for call in log.call_args_list if call.args[2]=='segment_prose')
        self.assertEqual(prose['location_schema'],1)

    def test_jsonl_replays_generated_prose_and_resolved_location_ids(self):
        state=StoryState(genre='драма',tone='лёгкий',target_length=20)
        events=['Комната','Порт']
        generated=[scene(),scene('Порт, причал','harbor','Мы вышли на портовый причал.','move')]
        tree={'root':{'segment_events':events,'segment_choice_texts':None,
                      'segment_children_ids':{},'origin_choice':None}}
        state.story_tree=tree
        class Client:
            apply_scene_to_state=staticmethod(DeepSeekClient.apply_scene_to_state)
            def generate_segment_prose(self,*args): return generated,{}
        with patch.object(self.server,'_log_debug') as log:
            asyncio.run(self.server._materialize_segment('test',None,state,'root',tree,Client()))
        entries=[{'step':'plan','genre':'драма','tone':'лёгкий','raw_plan':{}},
                 {'step':'characters','raw_characters':[]},
                 {'step':'segment_events','events':events,'ceiling_scene':2,'window':None}]
        entries += [{'step':call.args[2],**call.args[3]} for call in log.call_args_list]
        with patch('scene_locations._bind_background'):
            restored,_=self.server._reconstruct_session(json.loads(json.dumps(entries)))
        first=restored.story_tree[restored.tree_root_id]
        second=restored.story_tree[next(iter(first['children'].values()))]['scene']
        self.assertEqual(second['location_id'],'harbor')
        self.assertEqual(second['location_background_id'],'harbor_quay')
        self.assertEqual(second['narration'],'Мы вышли на портовый причал.')


if __name__=='__main__':
    unittest.main()
