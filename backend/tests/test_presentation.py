import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state_machine import StoryState
from deepseek_client import DeepSeekClient
from story_events import normalize_event
from prose_blocks import normalize_prose
from fallback_journal import FallbackJournal
from character_sprites import scene_sprites, assign_sprites
from visual_retrieval import VisualCatalog
import server


class PresentationTests(unittest.TestCase):
    def state(self):
        s = StoryState(genre='драма', tone='лёгкий', target_length=4)
        s.add_character(name='Алекс', age=30, role='герой', appearance='обычный', traits='спокойный',
                        is_protagonist=True, character_id='c1')
        s.add_character(name='Анна', age=28, role='врач', appearance='куртка', traits='решительная',
                        character_id='c2', appearance_features={'gender':'woman'})
        return s

    def test_blocks_survive_only_prose_call_materialization_save_replay_and_export(self):
        s = self.state()
        event = normalize_event({'v':3,'participants':['c1','c2'],
            'happening':{'subject':'c1','verb':'входит','object':'в комнату'},
            'location':{'id':'room','label':'Комната','background_id':'bedroom'},'ending':'victory'}, s.characters)
        raw = {'blocks':[
            {'type':'narration','text':'Мы вошли в комнату.'},
            {'type':'dialogue','speaker_id':'c2','text':'Привет.'},
            {'type':'thought','text':'Кажется, она ждала.'},
            {'type':'dialogue','speaker_id':'c1','text':'Добрый вечер.'}]}
        client=DeepSeekClient(api_key='offline-test')
        client._call=Mock(return_value=json.dumps({'scenes':[raw]}))
        tree={'root':{'segment_events':[event],'segment_choice_texts':None,'segment_children_ids':{}}}
        s.story_tree=tree;s.tree_root_id='root'
        with patch.object(server,'_log_debug') as log, patch.object(server,'_save_snapshot'):
            asyncio.run(server._materialize_segment('test',None,s,'root',tree,client))
        scene=tree['root']['scene']
        self.assertEqual([b['type'] for b in scene['blocks']],['narration','dialogue','thought','dialogue'])
        self.assertEqual(scene['location_background_id'],'bedroom')
        self.assertEqual(scene['blocks'][1]['speaker'],'Анна')
        self.assertIn('Привет.', str(s.recent_lines))
        client._call.assert_called_once()
        restored=StoryState.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(restored.story_tree['root']['scene']['blocks'],scene['blocks'])
        entries=[{'step':'plan','genre':s.genre,'tone':s.tone,'raw_plan':{}},
                 {'step':'characters','raw_characters':[vars(c) for c in s.characters.values()]},
                 {'step':'segment_events','events':[event],'ceiling_scene':1,'window':None}]
        entries += [{'step':call.args[2],**copy.deepcopy(call.args[3])} for call in log.call_args_list]
        with patch.object(DeepSeekClient,'_call',side_effect=AssertionError('No API replay')):
            replay,_=server._reconstruct_session(entries)
        self.assertEqual(replay.story_tree[replay.tree_root_id]['scene']['blocks'],scene['blocks'])
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'story.jsonl';p.write_text(json.dumps(scene,ensure_ascii=False),encoding='utf-8')
            with patch.object(server,'_transcript_path',return_value=p):
                text=server._render_transcript_markdown('test')
            self.assertIn('**Анна:** Привет.',text)
            self.assertIn('*Кажется, она ждала.*',text)
            self.assertEqual(text.count('Привет.'),1)

    def test_blocks_reject_unknown_speakers_but_accept_dialogue_only_scenes(self):
        s=self.state()
        valid=normalize_prose({'blocks':[{'type':'dialogue','speaker_id':'c2','text':'Да.'}]},s.characters)
        self.assertEqual(valid['narration'],'')
        client=DeepSeekClient(api_key='offline-test');client._call=Mock(return_value=json.dumps({'scenes':[valid]}))
        scenes,_=client.generate_segment_prose(s,[],None)
        self.assertTrue(scenes[0]['blocks'])
        with self.assertRaises(KeyError):normalize_prose({'blocks':[{'type':'dialogue','speaker_id':'bad','text':'Да.'}]},s.characters)

    def test_unknown_gender_and_mismatched_saved_sprites_never_use_random_gender(self):
        client=DeepSeekClient(api_key='offline-test')
        client._call=Mock(return_value=json.dumps({'characters':[{'name':'Незнакомец','age':30,'appearance':{}}]}))
        characters,_,_=client.generate_characters('История','драма','лёгкий',{},2,3)
        self.assertEqual(characters[0]['appearance_features']['gender'],'unknown')
        client._call.assert_called_once()
        s=self.state(); c=s.characters['c2'];c.appearance_features={}
        assign_sprites(s)
        self.assertFalse(c.sprite_id)
        self.assertEqual(scene_sprites(s,{'participants':['c2']})[0]['sprite']['reason'],'unknown_gender')
        c.appearance_features={'gender':'woman'}
        from character_sprites import get_sprite_catalog
        c.sprite_id=next(i for i,a in get_sprite_catalog().assets.items() if a['presentation']=='man')
        self.assertEqual(scene_sprites(s,{'participants':['c2']})[0]['sprite']['reason'],'incompatible_gender')
        c.sprite_id='removed_asset'
        self.assertEqual(scene_sprites(s,{'participants':['c2']})[0]['sprite']['reason'],'asset_unavailable')

    def test_explicit_art_selection_is_stable_and_not_inferred_from_text(self):
        catalog=VisualCatalog()
        self.assertTrue(catalog.recommend('Спальня')['backgrounds'][0]['is_fallback'])
        self.assertEqual(catalog.recommend('Порт',background_id='bedroom')['backgrounds'][0]['asset_id'],'bedroom')
        self.assertEqual(catalog.recommend('Порт',background_id='unknown')['fallback']['reason'],'invalid_background_id')
        s=self.state()
        first=DeepSeekClient.apply_scene_to_state(s,{'location_id':'room','location':'Комната','location_background_id':'bedroom','narration':'Тишина.'})
        second=DeepSeekClient.apply_scene_to_state(s,{'location_id':'room','location_background_id':'mine','narration':'Тишина.'})
        self.assertEqual(first['location_background_id'],second['location_background_id'])
        self.assertIn('abstract_space',catalog.prompt_catalog())

    def test_journal_deduplicates_reads_and_separates_content_from_technical_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=FallbackJournal(Path(directory)/'log.sqlite3')
            record=dict(session_id='test',node_id='n1',kind='character',subject='c2',reason='unknown_gender')
            self.assertTrue(journal.record(**record));self.assertFalse(journal.record(**record))
            self.assertTrue(journal.record(**{**record,'reason':'image_load_failed'}))
            self.assertTrue(journal.record(**{**record,'node_id':'n2'}))
            report=journal.report()
            self.assertEqual(report['scene_count'],3)
            self.assertEqual({g['category'] for g in report['groups']},{'content','technical'})
            self.assertEqual(max(g['count'] for g in report['groups']),2)


if __name__=='__main__':unittest.main()
