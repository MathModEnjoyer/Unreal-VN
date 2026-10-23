import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_appearance import (APPEARANCE_POOL, normalize_appearance, describe_appearance,
                                  appearance_image_prompt, appearance_pool_prompt, colab_profiles,
                                  colab_look, appearance_distance, LOOKS_PER_PROFILE)
from deepseek_client import DeepSeekClient
from prompts import build_character_planning_prompt
from state_machine import StoryState
from sync_character_appearance import BEGIN, sync
import server


class CharacterAppearanceTests(unittest.TestCase):
    def test_selected_ids_are_preserved_and_described_in_both_languages(self):
        selected = normalize_appearance({}, seed='test', gender='woman', outfit='work')
        selected.update({'hair':'long','skin':'tan','build':'full','hair_color':'auburn',
                         'eyes':'green','detail':'glasses'})
        self.assertEqual(normalize_appearance(selected, seed='different'), selected)
        for text in ('женщина','длинные','смуглая','рабочая одежда','полное','рыжие','зелёные','простые очки'):
            self.assertIn(text, describe_appearance(selected))
        for text in ('35-year-old adult woman','long hairstyle','tan skin','wearing',
                     'full-bodied build','auburn hair','green eyes','simple eyeglasses'):
            self.assertIn(text, appearance_image_prompt(selected, 35))

    def test_invalid_missing_and_nested_values_are_completed_without_review_calls(self):
        invalid = {'eyes':'purple', 'outfit':[], 'gender':{}, 'skin':None, 'hair':123}
        for source in (invalid, None, '', [], 123):
            with self.subTest(source=source):
                first = normalize_appearance(source, seed='stable')
                self.assertEqual(first, normalize_appearance(source, seed='stable'))
                self.assertEqual(set(first), set(APPEARANCE_POOL))
                for group, key in first.items():
                    self.assertIn(key, APPEARANCE_POOL[group])
        self.assertEqual(invalid['eyes'], 'purple')

    def test_legacy_response_ids_and_bald_color_are_normalized_without_mutation(self):
        source = {'hair':'bald','hair_color':'auburn','skin':'dark'}
        features = normalize_appearance(source)
        self.assertEqual(source['hair_color'], 'auburn')
        self.assertEqual(features['hair_color'], 'none')
        self.assertEqual(features['skin'], 'dark_brown')
        self.assertNotIn('auburn hair', appearance_image_prompt(features, 60))
        self.assertNotIn('цвет волос', describe_appearance(features))
        self.assertEqual(normalize_appearance({'hairstyle':'bun'})['hair'], 'bun')
        self.assertEqual(normalize_appearance({'hair':'tied'})['hair'], 'ponytail')
        for shade, expected in (('dark','black'),('light','blond'),('white','white')):
            self.assertEqual(normalize_appearance({'hair':'long','hair_color':shade})['hair_color'],expected)
        for value in ('none',None,[],{}):
            features = normalize_appearance({'hair':'short','hair_color':value},seed='stable')
            self.assertNotEqual(features['hair_color'], 'none')

    def test_cast_prompt_contains_all_twelve_groups(self):
        self.assertEqual(len(APPEARANCE_POOL),12)
        prompt = build_character_planning_prompt('История','драма','лёгкий',{},2,3)
        for field in APPEARANCE_POOL:
            self.assertIn('"' + field + '"', prompt)
        self.assertNotIn('"hairstyle"', prompt)

    def test_distance_prioritizes_visible_changes_and_is_symmetric(self):
        base = {'age':30,'appearance_features':normalize_appearance({
            'hair':'short','hair_color':'black','build':'average','eyes':'brown'})}
        eye, body, color = copy.deepcopy(base), copy.deepcopy(base), copy.deepcopy(base)
        eye['appearance_features']['eyes'] = 'green'
        body['appearance_features']['build'] = 'full'
        color['appearance_features']['hair_color'] = 'dark_brown'
        self.assertEqual(appearance_distance(base,base),0)
        self.assertGreater(appearance_distance(base,body),appearance_distance(base,eye))
        self.assertGreater(appearance_distance(base,body),appearance_distance(base,color))
        self.assertEqual(appearance_distance(base,body),appearance_distance(body,base))

    def test_bounded_library_has_unique_looks_age_coverage_and_applicable_traits(self):
        for extra, total in ((False,144),(True,240)):
            keys = []
            for profile in colab_profiles(extra):
                looks = [colab_look(i,gender=profile['gender'],outfit=profile['outfit_id'],seed='test')
                         for i in range(LOOKS_PER_PROFILE)]
                bands = [0 if look['age']<35 else 1 if look['age']<50 else 2 for look in looks]
                self.assertEqual([bands.count(i) for i in range(3)],[4,4,4])
                for look in looks:
                    f = look['appearance_features']
                    self.assertEqual(f['outfit'],profile['outfit_id'])
                    self.assertEqual(f['gender'],profile['gender'])
                    if f['hair']=='bald':
                        self.assertEqual(f['hair_color'],'none')
                    else:
                        self.assertNotEqual(f['hair_color'],'none')
                    if f['hair_color'] in ('gray','white'):
                        self.assertGreaterEqual(look['age'],45)
                    if profile['gender']=='woman':
                        self.assertEqual(f['facial_hair'],'none')
                    keys.append(tuple(f.items()))
            self.assertEqual(len(keys),total)
            self.assertEqual(len(set(keys)),total)

    def test_sampler_is_seeded_prefix_stable_and_does_not_share_mutable_results(self):
        kwargs = dict(gender='man',outfit='casual',seed='a')
        first = [colab_look(i,**kwargs) for i in range(4)]
        full = [colab_look(i,**kwargs) for i in range(12)]
        self.assertEqual(first,full[:4])
        first[0]['appearance_features']['eyes'] = 'INVALID'
        self.assertEqual(colab_look(0,**kwargs),full[0])
        self.assertNotEqual(full[0],colab_look(0,gender='man',outfit='casual',seed='b'))
        for invalid in (-1,12,None,'1',True):
            with self.assertRaises(ValueError):
                colab_look(invalid,gender='woman',outfit='casual')

    def test_cast_uses_pool_in_existing_single_request(self):
        features = normalize_appearance({}, seed='character', gender='man', outfit='coat')
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'characters':[
            {'name':'Антон','age':40,'traits':['спокойный'],'appearance':features}], 'relationships':[]}))
        chars, _, _ = client.generate_characters('История','драма','лёгкий',{},2,3)
        client._call.assert_called_once()
        self.assertEqual(chars[0]['appearance_features'], features)
        self.assertEqual(chars[0]['appearance'], describe_appearance(features))
        self.assertIsInstance(chars[0]['appearance'], str)
        prompt = client._call.call_args.kwargs['messages'][0]['content']
        self.assertIn(appearance_pool_prompt(), prompt)
        self.assertNotIn('Одна фраза, свободный текст', prompt)

    def test_missing_features_do_not_trigger_another_generation(self):
        client = DeepSeekClient(api_key='test')
        client._call = Mock(return_value=json.dumps({'characters':[
            {'name':'Анна','age':22,'appearance':'unexpected free text'}]}))
        chars, _, _ = client.generate_characters('История','драма','лёгкий',{},2,3)
        client._call.assert_called_once()
        self.assertEqual(set(chars[0]['appearance_features']), set(APPEARANCE_POOL))
        self.assertNotIn('unexpected free text', chars[0]['appearance'])

    def test_save_and_branch_copy_preserve_features_without_changing_legacy_text(self):
        state = StoryState(genre='драма', tone='лёгкий', target_length=6)
        features = normalize_appearance({}, seed='example')
        state.add_character('Анна',16,appearance=describe_appearance(features),appearance_features=features)
        features['hair'] = 'INVALID'
        restored = StoryState.from_dict(state.to_dict())
        self.assertEqual(restored.characters['c1'].age, 18)
        self.assertNotEqual(restored.characters['c1'].appearance_features['hair'], 'INVALID')
        old = {'eyes':'green','face':'round','hairstyle':'bun','skin':'brown'}
        snapshot_v1 = state.to_dict()
        snapshot_v1['characters']['c1']['appearance_features'] = copy.deepcopy(old)
        snapshot_v1['characters']['c1']['appearance'] = 'Старый подробный образ.'
        old_character = StoryState.from_dict(snapshot_v1).characters['c1']
        self.assertEqual(old_character.appearance_features, old)
        self.assertEqual(old_character.appearance, 'Старый подробный образ.')
        snapshot = restored.to_dict()
        snapshot['characters']['c1'].pop('appearance_features')
        snapshot['characters']['c1']['appearance'] = 'Старое произвольное описание.'
        legacy = StoryState.from_dict(snapshot).characters['c1']
        self.assertEqual(legacy.appearance, 'Старое произвольное описание.')
        self.assertEqual(legacy.appearance_features, {})

    def test_new_cast_and_jsonl_import_keep_metadata(self):
        features = normalize_appearance({}, seed='cast')
        character = {'name':'Анна','age':30,'appearance':describe_appearance(features),
                     'appearance_features':features}
        state = StoryState(genre='драма', tone='лёгкий', target_length=6)
        req = server.StartRequest(seed='История',genre='драма',tone='лёгкий',target_length=6)
        client = Mock()
        client.generate_characters.return_value = ([character],[],{})
        async def tree(work_state, client, nodes, *args):
            nodes['root'] = {'scene':{'narration':'Конец'},'children':{}}
            return 'root'
        with patch.object(server,'_client',return_value=client), patch.object(server,'_log_debug'), \
             patch.object(server,'_save_snapshot'), patch.object(server,'_build_tree_events',side_effect=tree), \
             patch.dict(server.TREE_BUILD_STATUS,clear=True):
            asyncio.run(server._build_tree_background('cast-test',state,req))
        self.assertEqual(state.characters['c1'].appearance_features,features)
        entries = [{'step':'plan','raw_plan':{},'genre':'драма','tone':'лёгкий','target_length':6},
                   {'step':'characters','raw_characters':[copy.deepcopy(character)]},
                   {'step':'segment_events','events':['Конец'],'ceiling_scene':6,'window':None}]
        restored, _ = server._reconstruct_session(entries)
        self.assertEqual(restored.characters['c1'].appearance_features,features)

    def test_colab_embeds_same_pool_and_extends_without_replacing_base_batch(self):
        project = Path(__file__).resolve().parents[2]
        notebook = json.loads((project/'StoryEngine_Anima_Characters_Colab.ipynb').read_text(encoding='utf-8'))
        shared = (project/'backend/character_appearance.py').read_text(encoding='utf-8')
        cells = [''.join(c['source']) for c in notebook['cells']]
        self.assertTrue(cells[9].startswith(BEGIN + shared))
        sync(project, check=True)
        with tempfile.TemporaryDirectory() as directory:
            ns = {'Path':Path,'uuid':uuid,'json':json,'os':os,'ROOT':Path(directory),
                  'print':lambda *args, **kwargs:None}
            statements = [item for item in ast.parse(cells[1]).body if isinstance(item,(ast.Assign,ast.Assert))]
            exec(compile(ast.Module(statements,type_ignores=[]),'config','exec'),ns)
            ns['MODEL_FILES'] = []
            self.assertTrue(ns['INCLUDE_EXTRA_OUTFITS'])
            exec(cells[9],ns)
            self.assertEqual(len(ns['JOBS']),240)
            ns['INCLUDE_EXTRA_OUTFITS'] = False
            exec(cells[9],ns)
            self.assertEqual(len(ns['JOBS']),144)
            run = ns['RUN']
            base = copy.deepcopy(ns['BY_ID'])
            for job in ns['JOBS']:
                self.assertEqual(job['appearance'],describe_appearance(job['appearance_features']))
                self.assertIn(job['appearance_features']['outfit'],APPEARANCE_POOL['outfit'])
            ns['INCLUDE_EXTRA_OUTFITS'] = True
            exec(cells[9],ns)
            self.assertEqual(len(ns['JOBS']),240)
            self.assertEqual(ns['RUN'],run)
            for key, job in base.items():
                self.assertEqual(ns['BY_ID'][key],job)


if __name__ == '__main__':
    unittest.main()
