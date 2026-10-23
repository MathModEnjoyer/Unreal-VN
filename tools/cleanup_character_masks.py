"""Repair the reviewed 27 masks, using Pillow/NumPy only (no image generation).

The parameters belong to run a31a976939692d07, 640x960 PNGs. Input is an
unmodified bank; output is a separate directory. Never overwrite the sources.
"""
from pathlib import Path
from collections import deque
import argparse
import hashlib
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# y boundary, maximum source luminance of the closed shoe outline.
FEET = {
 'man_casual_001': (880,105), 'man_coat_005':(875,110),
 'man_coat_007':(855,85), 'man_dress_011':(860,100),
 'man_medic_010':(875,90), 'man_medic_012':(845,95),
 'man_robe_004':(865,100), 'man_robe_010':(855,110),
 'man_space_009':(855,110), 'man_uniform_005':(860,100),
 'man_uniform_007':(845,100), 'man_work_008':(865,115),
 'woman_casual_002':(900,115), 'woman_casual_004':(900,165),
 'woman_casual_005':(895,125), 'woman_dress_005':(850,115),
 'woman_dress_008':(885,100), 'woman_formal_005':(860,110),
 'woman_medic_003':(855,125), 'woman_robe_006':(855,120),
 'woman_space_001':(820,100), 'woman_space_003':(875,110),
 'woman_space_007':(875,105), 'woman_traveler_005':(870,110),
 'woman_work_001':(860,120), 'woman_work_012':(875,110),
}
# Gap polygons enclose only unwanted background; threshold preserves dark edges.
GAPS = {
 'man_medic_010':([(308,675),(336,675),(340,920),(304,920)],145),
 'man_uniform_007':([(311,640),(329,640),(358,920),(280,920)],150),
 'woman_casual_004':([(297,540),(337,540),(342,847),(290,847)],185),
 'woman_formal_005':([(290,530),(337,530),(354,920),(278,920)],170),
 'woman_medic_003':([(296,655),(335,655),(355,920),(282,920)],155),
 'woman_space_001':([(307,640),(327,640),(354,920),(289,920)],140),
 'woman_work_001':([(280,660),(360,660),(360,920),(280,920)],195),
 'woman_dress_008':([(310,800),(329,800),(329,909),(310,909)],200),
 'woman_space_003':([(290,760),(360,760),(360,900),(290,900)],190),
 'man_dress_011':([(310,790),(328,790),(328,890),(310,890)],219),
 'woman_casual_002':([(308,865),(335,865),(335,930),(308,930)],195),
 'woman_dress_005':([(310,820),(328,820),(328,890),(310,890)],210),
}

def fill_holes(mask):
    """Flood from the patch border; enclosed light areas belong to footwear."""
    h,w=mask.shape
    exterior=np.zeros_like(mask)
    q=deque()
    for y,x in [(0,x) for x in range(w)]+[(h-1,x) for x in range(w)]+[(y,0) for y in range(h)]+[(y,w-1) for y in range(h)]:
        if not mask[y,x] and not exterior[y,x]: exterior[y,x]=True; q.append((y,x))
    while q:
        y,x=q.popleft()
        for ny,nx in ((y-1,x),(y+1,x),(y,x-1),(y,x+1)):
            if 0<=ny<h and 0<=nx<w and not mask[ny,nx] and not exterior[ny,nx]:
                exterior[ny,nx]=True; q.append((ny,nx))
    return ~exterior

def repair(id, original, cutout):
    rgba=np.array(cutout.convert('RGBA'))
    src=np.array(original.convert('RGB'))
    lum=src.mean(axis=2)
    alpha=rgba[:,:,3].copy()
    if id in GAPS:
        points,threshold=GAPS[id]
        region=Image.new('L',cutout.size); ImageDraw.Draw(region).polygon(points,fill=255)
        use=np.array(region)>0
        width=5 if id in {'woman_work_001','man_dress_011'} else 18
        edge=np.clip((threshold-lum)*255/width,0,255).astype('uint8')
        alpha[use]=np.minimum(alpha[use],edge[use])
    if id in FEET:
        y,threshold=FEET[id]
        dark=(lum[y:]<threshold)&(rgba[y:,:,3]>0)
        if id=='woman_robe_006':
            warm=src[y:,:,0].astype('int16')-src[y:,:,2].astype('int16')>35
            dark|=warm&(rgba[y:,:,3]>0)
        # Close one-pixel compression/antialias gaps in the drawn outline.
        closed=Image.fromarray(dark.astype('uint8')*255).filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
        outline=np.array(closed)>0
        outline[0,:]=alpha[y,:]>128
        solid=fill_holes(outline)
        # A subpixel soft edge avoids jagged outlines without retaining floor blobs.
        matte=Image.fromarray(solid.astype('uint8')*255).filter(ImageFilter.GaussianBlur(.45))
        alpha[y:]=np.minimum(alpha[y:],np.array(matte))
    if id=='woman_uniform_004':
        # Restore only the face interior. Hair and the outer silhouette stay intact.
        region=Image.new('L',cutout.size)
        ImageDraw.Draw(region).polygon([(300,57),(327,57),(347,85),(347,165),(320,178),(280,161),(275,100)],fill=255)
        use=np.array(region)>0
        alpha[use]=255
        rgba[use,:3]=src[use]
    rgba[:,:,3]=alpha
    return Image.fromarray(rgba)

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bank',type=Path); p.add_argument('output',type=Path)
    args=p.parse_args()
    for protected in (args.bank/'originals', args.bank/'cutouts'):
        if args.output.resolve().is_relative_to(protected.resolve()):
            p.error('Output must not overwrite originals or input cutouts')
    args.output.mkdir(parents=True,exist_ok=True)
    report=[]
    for id in sorted(set(FEET)|{'woman_uniform_004'}):
        orig=args.bank/'originals'/f'{id}.png'; cut=args.bank/'cutouts'/f'{id}.png'
        original=Image.open(orig); before=Image.open(cut).convert('RGBA')
        assert original.size==before.size==(640,960)
        after=repair(id,original,before); dst=args.output/f'{id}.png'; after.save(dst)
        a=np.array(before); b=np.array(after)
        report.append(dict(id=id,original_sha256=sha(orig),before_sha256=sha(cut),after_sha256=sha(dst),
            changed_pixels=int(np.any(a!=b,axis=2).sum()),removed_alpha_pixels=int((b[:,:,3]<a[:,:,3]).sum()),
            restored_alpha_pixels=int((b[:,:,3]>a[:,:,3]).sum())))
    (args.output/'cleanup_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Repaired {len(report)} cutouts into {args.output}')

if __name__=='__main__': main()
