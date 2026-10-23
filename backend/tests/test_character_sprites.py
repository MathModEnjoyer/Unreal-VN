import asyncio,copy,hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from state_machine import StoryState
from character_sprites import SpriteCatalog,assign_sprites,scene_sprites,apply_character_scene,get_sprite_catalog
from story_events import normalize_event,bind_event,restore_event_path
from deepseek_client import DeepSeekClient
import server

def state():
 s=StoryState('драма','лёгкий',8,sprite_seed='stable')
 s.add_character('Анна',24,is_protagonist=True,appearance_features={'gender':'woman','outfit':'casual'})
 s.add_character('Анна',32,appearance_features={'gender':'woman','outfit':'casual'})
 s.add_character('Олег',40,appearance_features={'gender':'man','outfit':'coat'})
 return s

def event(**kw):return {'v':3,'participants':['c1','c2'],'happening':{'subject':'c2','verb':'говорит','object':'с героем'},**kw}

class SpriteTests(unittest.TestCase):
 def test_bank_integrity_and_distinct_saved_identity(self):
  catalog=get_sprite_catalog();self.assertEqual(len(catalog.assets),235)
  s=state();assign_sprites(s);ids=[c.sprite_id for c in s.characters.values()]
  self.assertEqual(len(set(ids)),3)
  restored=StoryState.from_dict(json.loads(json.dumps(s.to_dict())))
  assign_sprites(restored);self.assertEqual(ids,[c.sprite_id for c in restored.characters.values()])
  self.assertEqual(s.to_dict(),restored.to_dict())
  self.assertIn(s.characters['c2'].sprite_appearance,s.characters_block())
  empty=SpriteCatalog(metadata=Path('absent-file'));assign_sprites(restored,empty)
  self.assertEqual(ids,[c.sprite_id for c in restored.characters.values()])
 def test_old_snapshots_get_repeatable_seed(self):
  old=state().to_dict();old.pop('sprite_seed');old.pop('character_expressions')
  a=StoryState.from_dict(old);b=StoryState.from_dict(old);assign_sprites(a);assign_sprites(b)
  self.assertEqual([c.sprite_id for c in a.characters.values()],[c.sprite_id for c in b.characters.values()])
 def test_sparse_expressions_presence_and_no_future_leak(self):
  s=state()
  first=DeepSeekClient.apply_scene_to_state(s,bind_event({'narration':'Мы говорим.','speaker_id':'c2','dialogue':'Да.'},event()))
  second=DeepSeekClient.apply_scene_to_state(s,bind_event({'narration':'Она улыбнулась.'},event(expressions={'c2':'smile'})))
  third=DeepSeekClient.apply_scene_to_state(s,bind_event({'narration':'Тишина.'},event()))
  self.assertEqual(scene_sprites(s,first)[0]['expression'],'base')
  self.assertEqual(scene_sprites(s,second)[0]['expression'],'smile')
  self.assertEqual(scene_sprites(s,third)[0]['expression'],'smile')
  self.assertTrue(scene_sprites(s,first)[0]['speaking'])
  empty=apply_character_scene(s,{'_planned_event':event(participants=[],expressions={'c2':'tension'})})
  self.assertEqual(scene_sprites(s,empty),[]);self.assertEqual(s.character_expressions['c2'],'smile')
  reset=apply_character_scene(s,{'_planned_event':event(expressions={'c2':'base'})})
  self.assertEqual(reset['expressions']['c2'],'base')
 def test_expression_format_filters_offstage_unknown_and_unsupported(self):
  s=state();raw=event(expressions={'c1':'tension','c2':'smile','c3':'tension','unknown':'smile','Анна':'smile'})
  normalized=normalize_event(raw,s.characters)
  self.assertEqual(normalized['expressions'],{'c1':'tension','c2':'smile'})
  self.assertNotIn('expressions',normalize_event(event(expressions={'c2':'surprise'}),s.characters))
 def test_branch_restore_does_not_inherit_sibling_expression(self):
  s=state();s.tree_root_id='r'
  tree={'r':{'scene':{},'children':{'A':'a','B':'b'},'history_records':[{'scene':1,'event':event()}]},
        'a':{'scene':{},'children':{},'history_records':[{'scene':2,'event':event(expressions={'c2':'smile'})}]},
        'b':{'scene':{},'children':{},'history_records':[{'scene':2,'event':event(expressions={'c2':'tension'})}]}}
  restore_event_path(s,tree,'a',include_current=True);self.assertEqual(s.character_expressions,{'c2':'smile'})
  restore_event_path(s,tree,'b',include_current=True);self.assertEqual(s.character_expressions,{'c2':'tension'})
  restore_event_path(s,tree,'a');self.assertEqual(s.character_expressions,{})
 def test_offstage_speaker_and_legacy_speaker(self):
  s=state();assign_sprites(s)
  fields=apply_character_scene(s,{'speaker_id':'c3','_planned_event':event()})
  self.assertEqual([c['id'] for c in scene_sprites(s,{**fields,'speaker_id':'c3'})],['c2'])
  self.assertEqual(apply_character_scene(s,{'speaker_id':'c3'})['participants'],['c3'])
  self.assertEqual(apply_character_scene(s,{'speaker_id':'unknown','speaker':'Олег'})['participants'],[])
 def test_allowlisted_files_changed_hash_and_route(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);png=root/'man_casual_001.png';png.write_bytes(b'local-test');sha=hashlib.sha256(png.read_bytes()).hexdigest()
   metadata=root/'catalog.json';metadata.write_text(json.dumps({'characters':[dict(id='man_casual_001',sha256=sha,approved=True,size=[1,1],bounds=[0,0,1,1])]}))
   catalog=SpriteCatalog(metadata,root);name=f'man_casual_001-{sha}.png'
   self.assertEqual(catalog.file(name),png);self.assertIsNone(catalog.file('../'+name))
   self.assertIsNone(catalog.file(name.replace(sha,'0'*64)))
   with patch.object(server,'get_sprite_catalog',return_value=catalog):
    response=asyncio.run(server.character_file(name));self.assertEqual(response.media_type,'image/png')
    self.assertIn('immutable',response.headers['cache-control'])
    png.write_bytes(b'changed');self.assertIsNone(catalog.file(name))
    with self.assertRaises(server.HTTPException) as err:asyncio.run(server.character_file(name))
    self.assertEqual(err.exception.status_code,404)
 def test_frozen_catalog_and_png_lookup_without_originals(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);bank=root/'characters'/'cutouts';bank.mkdir(parents=True)
   png=bank/'man_casual_001.png';png.write_bytes(b'packed');sha=hashlib.sha256(png.read_bytes()).hexdigest()
   meta=root/'character_data';meta.mkdir();(meta/'catalog.json').write_text(json.dumps({'characters':[
    dict(id='man_casual_001',sha256=sha,approved=True,size=[1,1],bounds=[0,0,1,1])]}))
   with patch.object(sys,'_MEIPASS',str(root),create=True):
    catalog=SpriteCatalog();self.assertEqual(len(catalog.assets),1)
    self.assertEqual(catalog.file(f'man_casual_001-{sha}.png'),png)

 def test_materialized_snapshots_and_jsonl_replay(self):
  s=state();assign_sprites(s);events=[event(),event(expressions={'c2':'smile'}),event(expressions={'c2':'tension'},ending='victory')]
  s.story_tree={'root':{'segment_events':events,'segment_choice_texts':None,'segment_children_ids':{},'origin_choice':None}}
  s.tree_root_id='root'
  raw=[{'narration':'Слова.','speaker_id':'c2','dialogue':'Да.'},{'narration':'Улыбка.'},{'narration':'Пауза.'}]
  client=DeepSeekClient(api_key='test');client._call=Mock(return_value=json.dumps({'scenes':raw}))
  entries=[{'step':'plan','raw_plan':{},'genre':'драма','tone':'лёгкий','target_length':8},
   {'step':'characters','raw_characters':[vars(c) for c in s.characters.values()],
    'sprite_seed':s.sprite_seed,'sprite_assignments':{cid:{'sprite_id':c.sprite_id,'sprite_appearance':c.sprite_appearance} for cid,c in s.characters.items()}},
   {'step':'segment_events','events':events,'ceiling_scene':8,'window':None,'origin_choice':None,'raw_pivot_choices':None}]
  def log(sid,path,step,payload):entries.append({'step':step,**copy.deepcopy(payload)})
  with patch.object(server,'_log_debug',side_effect=log):asyncio.run(server._materialize_segment('test',None,s,'root',s.story_tree,client))
  self.assertEqual(scene_sprites(s,s.story_tree['root']['scene'])[0]['expression'],'base')
  with patch.object(DeepSeekClient,'_call',side_effect=AssertionError('No paid replay')):restored,_=server._reconstruct_session(json.loads(json.dumps(entries)))
  self.assertEqual([c.sprite_id for c in s.characters.values()],[c.sprite_id for c in restored.characters.values()])
  original=[n['scene']['expressions'] for n in s.story_tree.values() if n.get('scene')]
  replayed=[n['scene']['expressions'] for n in restored.story_tree.values() if n.get('scene')]
  self.assertEqual(original,replayed);client._call.assert_called_once()

if __name__=='__main__':unittest.main()
