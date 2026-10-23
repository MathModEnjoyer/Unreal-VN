import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_conditions import initial_conditions, advance_conditions
from state_machine import StoryState
from deepseek_client import DeepSeekClient
from scene_locations import restore_location_path
from prompts import build_story_plan_prompt, build_segment_prose_prompt


INITIAL = {'time_of_day':'morning','atmosphere':{'weather':'rain','intensity':.7,
           'temperature':'warm','season':'summer','climate':'temperate','humidity':'humid'}}


def raw_scene(**fields):
    return {'location':'Лесная тропа','location_id':'trail','location_transition':None,'location_background_id':'forest_path',
            'narration':'Мы остановились у дерева.','conditions_change':None,**fields}


def story():
    state = StoryState(genre='драма',tone='лёгкий',target_length=30)
    state.story_plan = {'initial_conditions':copy.deepcopy(INITIAL)}
    state.scene_conditions = initial_conditions(state.story_plan)
    return state


class PersistentConditionTests(unittest.TestCase):
    def test_plan_seeds_only_valid_global_conditions(self):
        plan = {'initial_conditions':{**INITIAL,'screen_events':[{'type':'flash'}],
                'atmosphere':{**INITIAL['atmosphere'],'ambient':'ash','ambient_source':'volcano'}}}
        result = initial_conditions(plan)
        self.assertEqual(result,INITIAL)
        result['atmosphere']['weather']='clear'
        self.assertEqual(plan['initial_conditions']['atmosphere']['weather'],'rain')
        for malformed in [None,[],{'initial_conditions':None},{'initial_conditions':{'time_of_day':[],
            'atmosphere':{'weather':{},'intensity':float('nan'),'season':'invalid'}}}]:
            self.assertEqual(initial_conditions(malformed),{})

    def test_unchanged_scenes_keep_morning_rain_without_repeating_fields(self):
        state=story()
        with patch.object(DeepSeekClient,'_call',side_effect=AssertionError('No extra model calls')):
            for _ in range(8):
                fields=DeepSeekClient.apply_scene_to_state(state,raw_scene())
                self.assertEqual(fields['time_of_day'],'morning')
                self.assertEqual(fields['atmosphere'],INITIAL['atmosphere'])
        self.assertEqual(state.scene_conditions,INITIAL)

    def test_partial_changes_update_only_requested_fields_and_clear_stops_rain(self):
        state=story()
        fields=DeepSeekClient.apply_scene_to_state(state,raw_scene(
            narration='К вечеру дождь усилился.',
            conditions_change={'time_of_day':'evening','atmosphere':{'intensity':1}}))
        self.assertEqual(fields['time_of_day'],'evening')
        self.assertEqual(fields['atmosphere']['weather'],'rain')
        self.assertEqual(fields['atmosphere']['season'],'summer')
        self.assertEqual(fields['atmosphere']['intensity'],1)
        ended=DeepSeekClient.apply_scene_to_state(state,raw_scene(
            conditions_change={'atmosphere':{'weather':'clear'}}))
        self.assertEqual(ended['atmosphere']['weather'],'clear')
        self.assertEqual(ended['atmosphere']['intensity'],0)
        self.assertEqual(ended['time_of_day'],'evening')
        DeepSeekClient.apply_scene_to_state(state,raw_scene())
        self.assertEqual(state.scene_conditions['atmosphere']['weather'],'clear')

    def test_malformed_or_absent_changes_hold_known_values(self):
        for delta in [None,[],True,'night',{}, {'time_of_day':[], 'atmosphere':{'weather':[],
                     'temperature':'lava','humidity':{},'intensity':float('inf')}}]:
            self.assertEqual(advance_conditions(INITIAL,{'conditions_change':delta}),INITIAL)
        state=story()
        fields=DeepSeekClient.apply_scene_to_state(state,raw_scene(time_of_day='night',
            atmosphere={'weather':'snow','temperature':'cold'}))
        self.assertEqual(fields['time_of_day'],'morning')
        self.assertEqual(fields['atmosphere']['weather'],'rain')
        self.assertEqual(advance_conditions(INITIAL,{}),INITIAL)
        empty=StoryState(genre='драма',tone='лёгкий',target_length=20)
        fields=DeepSeekClient.apply_scene_to_state(empty,raw_scene(time_of_day='night',atmosphere={'weather':'storm'}))
        self.assertNotIn('time_of_day',fields)
        self.assertNotIn('atmosphere',fields)

    def test_local_sources_and_flash_are_not_inherited(self):
        state=story()
        first=DeepSeekClient.apply_scene_to_state(state,raw_scene(
            atmosphere={'ambient':'ash','ambient_source':'volcano','ambient_intensity':.3},
            screen_events=[{'type':'flash','source':'explosion'}]))
        self.assertEqual(first['atmosphere']['ambient'],'ash')
        self.assertEqual(len(first['screen_events']),1)
        self.assertNotIn('screen_events',state.scene_conditions)
        second=DeepSeekClient.apply_scene_to_state(state,raw_scene(
            conditions_change={'atmosphere':{'ambient':'ash','ambient_source':'volcano'}}))
        self.assertNotIn('ambient',second['atmosphere'])
        self.assertNotIn('ambient_source',second['atmosphere'])
        self.assertNotIn('screen_events',second)
        self.assertEqual(second['atmosphere']['weather'],'rain')

    def test_save_and_independent_branches_keep_conditions(self):
        state=story()
        DeepSeekClient.apply_scene_to_state(state,raw_scene())
        saved=StoryState.from_dict(json.loads(json.dumps(state.to_dict())))
        alternative=copy.deepcopy(saved)
        DeepSeekClient.apply_scene_to_state(alternative,raw_scene(conditions_change={
            'time_of_day':'night','atmosphere':{'weather':'clear'}}))
        self.assertEqual(saved.scene_conditions,INITIAL)
        self.assertEqual(alternative.scene_conditions['time_of_day'],'night')
        round_trip=StoryState.from_dict(json.loads(json.dumps(alternative.to_dict())))
        self.assertEqual(round_trip.scene_conditions,alternative.scene_conditions)
        fields=DeepSeekClient.apply_scene_to_state(round_trip,raw_scene())
        self.assertEqual(fields['time_of_day'],'night')
        self.assertEqual(fields['atmosphere']['weather'],'clear')

    def test_missing_initial_plan_is_initialized_once_by_first_scene(self):
        state=StoryState(genre='драма',tone='лёгкий',target_length=20)
        self.assertEqual(state.scene_conditions,{})
        first=DeepSeekClient.apply_scene_to_state(state,raw_scene(conditions_change=INITIAL))
        second=DeepSeekClient.apply_scene_to_state(state,raw_scene())
        self.assertEqual(first['atmosphere'],second['atmosphere'])
        self.assertEqual(second['time_of_day'],'morning')
        data=state.to_dict(); data.pop('scene_conditions'); data['story_plan']={'initial_conditions':INITIAL}
        restored=StoryState.from_dict(data)
        self.assertEqual(restored.scene_conditions,INITIAL)

    def test_plan_and_prose_prompts_use_existing_calls_with_compact_changes(self):
        state=story(); client=DeepSeekClient(api_key='test-key')
        plan={'turning_points':[],'initial_conditions':INITIAL}
        with patch.object(client,'_call',return_value=json.dumps(plan)) as call:
            generated,_=client.generate_story_plan('Дождливое утро','драма','лёгкий',20,[],state)
        call.assert_called_once()
        self.assertEqual(initial_conditions(generated),INITIAL)
        self.assertIn('initial_conditions',build_story_plan_prompt('Дождливое утро','драма','лёгкий',20,[]))
        self.assertIn('ИСХОДНЫЕ УСЛОВИЯ', client._story_messages(state, "")[0]["content"])
        self.assertNotIn('НАЧАЛЬНЫЕ УСЛОВИЯ',state.to_events_prompt_block())
        with patch.object(client,'_call',return_value=json.dumps({'scenes':[raw_scene()]})) as call:
            scenes,_=client.generate_segment_prose(state,['Разговор'],None)
        call.assert_called_once()
        self.assertIsNone(scenes[0]['conditions_change'])
        prompt=build_segment_prose_prompt(['Разговор'],None)
        self.assertIn('conditions_change',prompt)
        self.assertIn('Не дублируй полное состояние',prompt)



class ConditionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home=tempfile.TemporaryDirectory()
        with patch('pathlib.Path.home',return_value=Path(cls.home.name)):
            import server
        cls.server=server

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def test_start_initializes_conditions_before_background_build(self):
        plan={'turning_points':[],'initial_conditions':INITIAL}
        client=SimpleNamespace(generate_story_plan=Mock(return_value=(plan,{})), moderate_text=Mock())
        remember=Mock(); background=AsyncMock()
        async def start():
            result=await self.server.start_story(self.server.StartRequest(
                seed='Дождливое утро',genre='драма',tone='лёгкий',target_length=20))
            task=self.server.TREE_BUILD_TASKS.pop(result['session_id'])
            await task
        with (patch.object(self.server,'_client',return_value=client),
              patch.object(self.server,'_remember_session',remember),
              patch.object(self.server,'_log_debug'),
              patch.object(self.server,'_build_tree_background',background)):
            asyncio.run(start())
        state=remember.call_args.args[1]
        self.assertEqual(state.scene_conditions,INITIAL)
        client.generate_story_plan.assert_called_once()
        background.assert_awaited_once()

    def test_entering_and_leaving_room_hides_rain_without_clearing_state(self):
        state=story()
        outside=DeepSeekClient.apply_scene_to_state(state,raw_scene())
        self.assertEqual(self.server._scene_with_visuals(state,outside)['visual']['effects']['weather']['type'],'rain')
        inside=DeepSeekClient.apply_scene_to_state(state,raw_scene(location='Спальня Анны',location_id='anna_room',location_background_id='bedroom',
            narration='Мы вошли в спальню Анны.',location_transition={'kind':'move','evidence':'Мы вошли в спальню Анны.'}))
        rendered=self.server._scene_with_visuals(state,inside)
        self.assertIsNone(rendered['visual']['effects']['weather'])
        self.assertEqual(inside['atmosphere']['weather'],'rain')
        self.assertEqual(rendered['visual']['effects']['grading']['strength'],.22)
        returned=DeepSeekClient.apply_scene_to_state(state,raw_scene(narration='Мы вернулись на лесную тропу.',
            location_transition={'kind':'move','evidence':'Мы вернулись на лесную тропу.'}))
        self.assertEqual(self.server._scene_with_visuals(state,returned)['visual']['effects']['weather']['type'],'rain')

    def test_resuming_other_branch_restores_ancestor_weather_not_last_played_branch(self):
        state=story(); root=DeepSeekClient.apply_scene_to_state(state,raw_scene())
        dry=DeepSeekClient.apply_scene_to_state(state,raw_scene(conditions_change={
            'time_of_day':'night','atmosphere':{'weather':'clear'}}))
        state.tree_root_id='root'
        tree={'root':{'scene':root,'children':{'A':'dry','B':'other'}},
              'dry':{'scene':dry,'children':{}},'other':{'scene':None,'children':{}}}
        restore_location_path(state,tree,'other')
        self.assertEqual(state.scene_conditions,INITIAL)
        restore_location_path(state,tree,'root')
        self.assertEqual(state.scene_conditions,INITIAL)

    def test_materialized_nodes_store_resolved_conditions_for_frontend_and_logs(self):
        state=story()
        scenes=[raw_scene(),raw_scene(narration='Наступила ночь.',conditions_change={'time_of_day':'night'}),raw_scene()]
        events=['Утро','Ночь','Разговор']
        tree={'root':{'segment_events':events,'segment_choice_texts':None,
                      'segment_children_ids':{},'origin_choice':None}}
        state.story_tree=tree
        class Client:
            apply_scene_to_state=staticmethod(DeepSeekClient.apply_scene_to_state)
            def generate_segment_prose(self,*args): return scenes,{}
        with patch.object(self.server,'_log_debug') as log:
            asyncio.run(self.server._materialize_segment('test',None,state,'root',tree,Client()))
        root=tree['root']; second=tree[next(iter(root['children'].values()))]
        third=tree[next(iter(second['children'].values()))]
        self.assertEqual(root['scene']['time_of_day'],'morning')
        self.assertEqual(third['scene']['time_of_day'],'night')
        self.assertEqual(third['scene']['atmosphere']['weather'],'rain')
        self.assertEqual(self.server._scene_with_visuals(state,third['scene'])['visual']['backgrounds'][0]['time_of_day'],'night')
        entries=[{'step':'plan','genre':'драма','tone':'лёгкий','raw_plan':state.story_plan},
                 {'step':'characters','raw_characters':[]},
                 {'step':'segment_events','events':events,'ceiling_scene':3,'window':None}]
        entries += [{'step':call.args[2],**call.args[3]} for call in log.call_args_list]
        restored,_=self.server._reconstruct_session(json.loads(json.dumps(entries)))
        self.assertEqual(restored.scene_conditions['time_of_day'],'night')
        self.assertEqual(restored.scene_conditions['atmosphere']['weather'],'rain')


if __name__=='__main__':
    unittest.main()
