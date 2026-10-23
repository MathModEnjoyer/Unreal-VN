"""Offline metadata builder. Pillow is not a game runtime dependency.
Only coarse garments are visually reviewed; fine prompt attributes are not facts.
Never modifies the source PNGs or face annotations.
"""
import hashlib,json
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
BANK=ROOT/'assets'/'characters'
DESCRIPTIONS={'casual':'повседневная кофта и брюки','coat':'длинное пальто поверх закрытой одежды',
'work':'рубашка с рабочим фартуком и брюками','traveler':'туника, короткая накидка и сапоги',
'medic':'длинный халат или пальто поверх закрытой одежды','formal':'сдержанная кофта или жакет с брюками',
'dress':'длинное простое платье','uniform':'простая закрытая одежда без заметных знаков различия',
'robe':'длинное свободное одеяние','space':'закрытый комбинезон и сапоги'}
AMBIGUOUS={'woman_medic_001','woman_medic_007','woman_space_006','woman_coat_005','woman_coat_012',
'woman_formal_001','woman_formal_002','woman_formal_012','woman_work_003','woman_robe_003','woman_robe_005',
'woman_robe_007','woman_traveler_004','woman_traveler_007','woman_traveler_009','woman_traveler_012'}
def build():
 source=json.loads((BANK/'character_catalog.json').read_text('utf-8'))
 landmarks=json.loads((BANK/'face_landmarks.json').read_text('utf-8'))['characters'];result=[]
 for c in source['characters']:
  cid=c['id'];gender,outfit,_=cid.split('_');path=BANK/'cutouts'/(cid+'.png')
  sha=hashlib.sha256(path.read_bytes()).hexdigest()
  assert sha==c['cutout_sha256']==landmarks[cid]['cutout_sha256'],cid
  with Image.open(path) as im:
   assert im.mode=='RGBA' and im.size==(640,960),cid
   bounds=im.getchannel('A').point(lambda a:255 if a>=32 else 0).getbbox()
  desc=DESCRIPTIONS[outfit];tags=[outfit]
  if outfit=='dress' and gender=='man':desc='длинная закрытая туника';tags=['robe','dress']
  if outfit=='medic':tags+=['coat']
  if outfit=='space':tags+=['uniform']
  if cid=='man_formal_008':desc='простая кофта и брюки';tags=['casual','work']
  anchor=landmarks[cid];face={k:anchor[k] for k in ('points_px','regions_px','review_flags')};face['size']=anchor['size']
  result.append(dict(id=cid,sha256=sha,size=[640,960],bounds=list(bounds),approved=True,
   presentation='ambiguous' if cid in AMBIGUOUS else gender,outfits=tags,generic=outfit in {'casual','coat','formal'},
   description='Взрослый персонаж; '+desc+'.',emotions_reviewed=True,face=face))
 dest=ROOT/'backend'/'character_data'/'catalog.json';dest.parent.mkdir(exist_ok=True)
 dest.write_text(json.dumps({'version':1,'characters':result},ensure_ascii=False,separators=(',',':'))+'\n','utf-8')
 print(f'{len(result)} runtime sprites')
if __name__=='__main__':build()
