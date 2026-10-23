"""Build approximate, visually guided face controls for the reviewed character bank.

Requires Pillow. This is an offline asset tool, not a neural face detector.
Manual anchors are in character_face_anchors.txt beside this script. Derived
regions are working areas, not exact skin/eyelid segmentation. Emotions require
separate visual testing. Input PNGs are never changed.
"""
from pathlib import Path
import argparse, hashlib, json
from PIL import Image, ImageDraw

GLASSES=set('''man_casual_005 man_coat_010 man_dress_005 man_formal_011 man_formal_012
man_medic_005 man_medic_010 man_medic_011 man_robe_003 man_space_004 man_space_006
man_space_009 man_traveler_001 man_traveler_003 man_uniform_005 man_uniform_008
man_uniform_009 man_work_002 man_work_009 man_work_010 woman_casual_002
woman_coat_001 woman_coat_011 woman_dress_012 woman_formal_001 woman_formal_008
woman_formal_011 woman_medic_001 woman_medic_006 woman_space_003 woman_space_007
woman_uniform_001 woman_work_002 woman_work_006 woman_work_010'''.split())
TURNED=set('man_space_005 man_robe_012 woman_space_006'.split())
WEAK_MOUTH={'woman_uniform_011','woman_dress_006'}
WEAK_EYES={'woman_medic_002'}
HAIR={'woman_casual_004'}
FACIAL_HAIR={
 'man_casual':(3,4,5,7,9,10,12), 'man_coat':(2,4,5,6,7),
 'man_dress':(2,3,5,7,9,10,11), 'man_formal':(2,3,4,5,6,9,10,11),
 'man_medic':(2,3,4,5,6,7,9,11), 'man_robe':(3,4,5,6,8,10),
 'man_space':(2,3,6,7,8,9,10), 'man_traveler':(1,3,4,5,6,9,11,12),
 'man_uniform':(2,3,6,7,8,10), 'man_work':(2,6,7,9,10,11,12),
}
FACIAL_HAIR={f'{group}_{n:03d}' for group,nums in FACIAL_HAIR.items() for n in nums}

def load_anchors(path):
    result={}; group=None
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if line.startswith('['): group=line.strip('[]');continue
        suffix,*numbers=line.split();id=f'{group}_{suffix}'
        if len(numbers)!=9 or id in result:raise ValueError(f'Invalid anchor row: {line}')
        result[id]=list(map(int,numbers))
    return result

def build(bank, rows):
    cat=json.loads((bank/'character_catalog.json').read_text(encoding='utf-8'))
    assert set(rows)=={c['id'] for c in cat['characters']},'Anchor/catalog IDs must match exactly'
    records={}
    for c in cat['characters']:
        id=c['id'];lx,ly,rx,ry,lb,rb,mx,my,mw=rows[id]
        path=bank/c['cutout'];digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=c['cutout_sha256']:raise ValueError(f'Source hash mismatch: {id}')
        im=Image.open(path).convert('RGBA');assert im.size==(640,960)
        points={'eye_left':[lx,ly],'eye_right':[rx,ry],
                'brow_left':[lx,lb],'brow_right':[rx,rb],
                'mouth_left':[mx-mw,my],'mouth_center':[mx,my],'mouth_right':[mx+mw,my]}
        assert lx<rx and lb<ly<my and rb<ry<my and mw>0,id
        for x,y in points.values():
            assert 0<=x<640 and 0<=y<960 and im.getpixel((x,y))[3]>=128,(id,x,y)
        span=rx-lx; ex=round(max(8,min(18,span*.31)))
        ey=round(max(4,min(12,((ly-lb)+(ry-rb))*.28)))
        regions={'eye_left':[lx-ex,ly-ey,lx+ex,ly+ey],
                 'eye_right':[rx-ex,ry-ey,rx+ex,ry+ey],
                 'brow_left':[lx-ex-2,lb-4,lx+ex+2,lb+4],
                 'brow_right':[rx-ex-2,rb-4,rx+ex+2,rb+4],
                 'mouth':[mx-mw-3,my-6,mx+mw+3,my+6],
                 'working_face':[round(lx-span*.65),round(min(lb,rb)-span*.6),
                                 round(rx+span*.65),round(my+span*.65)]}
        flags=[]
        for label,ids in [('glasses',GLASSES),('head_turned',TURNED),('mouth_low_detail',WEAK_MOUTH),
                          ('eyes_stylized',WEAK_EYES),('hair_near_eye',HAIR),
                          ('facial_hair_near_mouth',FACIAL_HAIR)]:
            if id in ids:flags.append(label)
        records[id]={'cutout':c['cutout'],'cutout_sha256':digest,'size':[640,960],
            'points_px':points,'points_normalized':{k:[round(x/640,6),round(y/960,6)] for k,(x,y) in points.items()},
            'regions_px':regions,'review_flags':flags,'anchor_method':'manual_visual_approximation',
            'regions_method':'derived_from_anchors','review_status':'pending_overlay_review',
            'emotion_ready':False,'requires_emotion_preview':True,'baseline_expression_verified':False}
    return {'version':1,'coordinate_system':{'origin':'top_left','x_axis':'right','y_axis':'down',
        'left_right':'viewer','rectangle':'x0_y0_x1_y1_exclusive','normalization':'x/width,y/height'},
        'description':'Approximate face control anchors; not a dense facial mesh or exact feature segmentation.',
        'characters':records}

def sheets(bank,data,folder):
    folder.mkdir(parents=True,exist_ok=True)
    groups={}
    for id,r in data['characters'].items():groups.setdefault(id.rsplit('_',1)[0],[]).append((id,r))
    for group,records in groups.items():
        sheet=Image.new('RGB',(1200,1020),'#dee1df');d=ImageDraw.Draw(sheet)
        for i,(id,r) in enumerate(records):
            x=i%4*300;y=i//4*340
            sprite=Image.open(bank/r['cutout']).convert('RGBA').crop((200,0,440,240)).resize((300,300))
            bg=Image.new('RGBA',sprite.size,(95,110,115,255));bg.alpha_composite(sprite)
            sheet.paste(bg.convert('RGB'),(x,y))
            for key,(px,py) in r['points_px'].items():
                sx=x+(px-200)*1.25;sy=y+py*1.25
                color='#2dffff' if key.startswith('eye') else '#fff23c' if key.startswith('brow') else '#ff3b88'
                d.ellipse((sx-2,sy-2,sx+2,sy+2),outline=color,width=2)
            d.text((x+3,y+302),id,fill='black')
            d.text((x+3,y+318),', '.join(r['review_flags']),fill='black')
        sheet.save(folder/(group+'.jpg'),quality=96)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bank',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--anchors',type=Path,default=Path(__file__).with_name('character_face_anchors.txt'))
    p.add_argument('--sheets',type=Path)
    args=p.parse_args()
    protected=[args.bank/'character_catalog.json']+list((args.bank/'cutouts').glob('*.png'))+list((args.bank/'originals').glob('*.png'))
    if args.output.resolve() in {p.resolve() for p in protected}:p.error('Output would overwrite source assets')
    data=build(args.bank,load_anchors(args.anchors))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.sheets:sheets(args.bank,data,args.sheets)
    print(f"Built {len(data['characters'])} face records; overlay review pending.")

if __name__=='__main__':main()
