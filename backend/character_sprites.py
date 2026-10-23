"""Reviewed local sprite bank, persistent assignments and per-scene state.

No image inference, generated emotion PNGs, network calls or model requests.
"""
import hashlib
import json
import os
import random
import re
import sys
from functools import lru_cache
from pathlib import Path
from character_identity import resolve_ref

EXPRESSIONS = {'base','smile','tension'}


def sprite_directory():
    if os.getenv('STORY_ENGINE_CHARACTER_DIR'):
        return Path(os.environ['STORY_ENGINE_CHARACTER_DIR'])
    if getattr(sys,'_MEIPASS',None):
        return Path(sys._MEIPASS)/'characters'/'cutouts'
    return Path(__file__).resolve().parents[1]/'assets'/'characters'/'cutouts'


class SpriteCatalog:
    def __init__(self, metadata=None, directory=None):
        self.directory=Path(directory) if directory is not None else sprite_directory()
        metadata=Path(metadata) if metadata is not None else Path(__file__).with_name('character_data')/'catalog.json'
        if getattr(sys,'_MEIPASS',None) and metadata.parent.name=='character_data':
            metadata=Path(sys._MEIPASS)/'character_data'/'catalog.json'
        self.assets={}
        try: entries=json.loads(metadata.read_text(encoding='utf-8')).get('characters',[])
        except (OSError,ValueError):return
        if not isinstance(entries,list):return
        for item in entries:
            if not isinstance(item,dict) or not re.fullmatch(r'[a-z_]+_\d{3}',str(item.get('id',''))):continue
            cid=item['id']; path=self.directory/(cid+'.png')
            size=item.get('size');bounds=item.get('bounds')
            if not (isinstance(size,list) and len(size)==2 and all(type(n) is int and 0<n<=1536 for n in size)
                    and isinstance(bounds,list) and len(bounds)==4 and all(type(n) is int for n in bounds)
                    and 0<=bounds[0]<bounds[2]<=size[0] and 0<=bounds[1]<bounds[3]<=size[1]):continue
            if not item.get('approved') or path.is_symlink():continue
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:continue
            except (OSError,KeyError):continue
            self.assets[cid]=item

    def descriptor(self,asset_id):
        item=self.assets.get(asset_id)
        if not item:return None
        return {'asset_id':asset_id,'sha256':item['sha256'],
                'url':f"/characters/{asset_id}-{item['sha256']}.png",
                'size':item['size'],'bounds':item['bounds'],
                'face':item.get('face') if item.get('emotions_reviewed') else None}

    def file(self,filename):
        match=re.fullmatch(r'([a-z_]+_\d{3})-([a-f0-9]{64})\.png',filename)
        if not match:return None
        item=self.assets.get(match[1])
        if not item or item['sha256']!=match[2]:return None
        path=self.directory/(match[1]+'.png')
        try:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=match[2]:return None
        except OSError:return None
        return path


@lru_cache(maxsize=1)
def get_sprite_catalog():return SpriteCatalog()


def assign_sprites(state,catalog=None):
    catalog=catalog if catalog is not None else get_sprite_catalog()
    cast=getattr(state,"characters",{})
    used={c.sprite_id for c in cast.values() if c.sprite_id}
    for cid,c in cast.items():
        if c.kind == 'nonhuman':continue
        if c.sprite_id:continue  # Missing art never changes a saved identity.
        features=c.appearance_features or {}
        pool=[a for a in catalog.assets.values() if a['id'] not in used]
        gender=features.get('gender')
        if gender not in {"man", "woman"}:
            continue
        pool=[a for a in pool if a.get("presentation")==gender]
        if not pool:continue
        outfit=features.get('outfit')
        matched=[a for a in pool if outfit in a.get('outfits',[])]
        if matched:pool=matched
        else:
            # Generic clothing is safer than a space suit in a modern room.
            generic=[a for a in pool if a.get('generic')]
            if generic:pool=generic
        rng=random.Random(f'{state.sprite_seed}:{cid}')
        item=rng.choice(sorted(pool,key=lambda a:a['id']))
        c.sprite_id=item['id'];c.sprite_appearance=str(item.get('description') or '')
        used.add(c.sprite_id)


def expression_changes(raw,cast):
    if not isinstance(raw,dict):return {}
    result={}
    for ref,value in raw.items():
        cid=resolve_ref(cast,ref)
        if cid and isinstance(value,str) and value in EXPRESSIONS:result[cid]=value
    return result


def apply_character_scene(state,scene):
    """Planning owns presence and expression deltas. Speaker may be offstage."""
    event=scene.get('_planned_event')
    if isinstance(event,dict):
        present=event.get('participants',[])
        changes=expression_changes(event.get('expressions'),state.characters)
    else:
        # Old text-only scenes had no explicit presence; show just a known
        # speaker. Never infer everyone else from narration or past scenes.
        sid=scene.get('speaker_id')
        if 'speaker_id' not in scene:sid=resolve_ref(state.characters,scene.get('speaker'),aliases=True)
        present=[sid] if isinstance(sid,str) and sid in state.characters else []
        changes={}
    ids=[]
    for ref in present if isinstance(present,list) else []:
        cid=resolve_ref(state.characters,ref)
        if cid and cid not in ids:ids.append(cid)
    # Offstage characters cannot acquire an expression from this scene.
    state.character_expressions.update({cid:value for cid,value in changes.items() if cid in ids})
    return {'participants':ids,'expressions':{cid:state.character_expressions.get(cid,'base') for cid in ids}}


def scene_sprites(state,scene,catalog=None):
    catalog=catalog if catalog is not None else get_sprite_catalog()
    # A fully materialized segment puts state ahead of what the user reads.
    # Always use the node's snapshot, never the final expression state.
    cast=getattr(state,"characters",{})
    present=scene.get('participants')
    if not isinstance(present,list):present=[scene.get('speaker_id')]
    expressions=scene.get('expressions',{})
    result=[]
    for cid in present:
        c=cast.get(cid) if isinstance(cid,str) else None
        if not c or c.is_protagonist:continue  # First-person visual novel.
        asset=catalog.descriptor(c.sprite_id)
        gender=(c.appearance_features or {}).get('gender')
        stored=catalog.assets.get(c.sprite_id, {})
        reason = None
        if c.kind == 'nonhuman':
            reason='nonhuman_character'
        elif gender not in {'man', 'woman'}:
            reason='unknown_gender'
        elif asset and stored.get('presentation') != gender:
            reason='incompatible_gender'
        elif not asset:
            reason='asset_unavailable' if c.sprite_id else 'no_compatible_sprite'
        if reason:
            asset={'asset_id':'character_placeholder','is_fallback':True,'reason':reason,
                   'size':[512,768],'bounds':[0,0,512,768],'face':None}
        if asset:
            value=expressions.get(cid,'base') if isinstance(expressions,dict) else 'base'
            result.append({'id':cid,'name':c.name,'sprite':asset,
                           'expression':value if value in EXPRESSIONS else 'base',
                           'speaking':scene.get('speaker_id')==cid})
    return result
