import { backgroundTime, backgroundPalette, gradePixels, gradeAtmosphere } from './backgroundProcessing.js';

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
export const EXPRESSIONS = ['base', 'smile', 'tension'];

export function validFace(face, width, height) {
  if (!face || !Array.isArray(face.size) || face.size[0] !== width || face.size[1] !== height) return false;
  const p = face.points_px, r = face.regions_px?.working_face;
  if (!p || !Array.isArray(r) || r.length !== 4 || !r.every(Number.isFinite) ||
      r[0] < 0 || r[1] < 0 || r[2] > width || r[3] > height || r[2] <= r[0] || r[3] <= r[1]) return false;
  for (const key of ['eye_left','eye_right','brow_left','brow_right','mouth_left','mouth_center','mouth_right']) {
    const v = p[key];
    if (!Array.isArray(v) || v.length !== 2 || !v.every(Number.isFinite) ||
        v[0] < 0 || v[0] >= width || v[1] < 0 || v[1] >= height) return false;
  }
  return p.eye_right[0] - p.eye_left[0] >= 8;
}

// Always sample the unchanged original. Alpha, silhouette and all pixels
// outside working_face remain byte-identical. No invented teeth or mouth.
export function deformFace(source, width, height, face, expression = 'base') {
  const out = new Uint8ClampedArray(source);
  if (!['smile','tension'].includes(expression) || !validFace(face,width,height) || source.length !== width*height*4) return out;
  const p = face.points_px, flags = face.review_flags || [];
  const span = p.eye_right[0]-p.eye_left[0], scale = span/40;
  const strength = .8 * (flags.includes('head_turned') ? .55 : 1);
  const taper = (q, flat) => (1+Math.cos(Math.PI*clamp((Math.abs(q)-flat)/(1-flat),0,1)))/2;
  const window = (x,y,cx,cy,rx,ry) => taper((x-cx)/rx,.6)*taper((y-cy)/ry,.12);
  const [x0,y0,x1,y1] = face.regions_px.working_face.map(Math.round);
  for (let y=y0;y<y1;y++) for(let x=x0;x<x1;x++) {
    const dst=(y*width+x)*4;
    if(source[dst+3]<240) continue;
    let dy=0;
    for(const [side,direction] of [['left',1],['right',-1]]) {
      const [ex,ey]=p['eye_'+side], [bx,by]=p['brow_'+side];
      const inner=direction*(x-bx)/(span*.29), cutoff=ey-Math.max(7,span*.20);
      let w=window(x,y,bx,by,span*.43,Math.max(9,span*.29));
      w*=(1+Math.cos(Math.PI*clamp((y-(by+1))/Math.max(1,cutoff-(by+1)),0,1)))/2;
      const brow=(expression==='smile' ? -1.2+.5*inner : 1+3.4*clamp(inner,-1,1))*scale;
      let delta=brow*w;
      delta*=(1+Math.cos(Math.PI*clamp((y-delta-(cutoff-3))/3,0,1)))/2;
      dy+=delta;
      if(!flags.includes('glasses')&&!flags.includes('eyes_stylized')) {
        dy+=(y-ey)*(expression==='smile'?-.18:-.12)*window(x,y,ex,ey,span*.35,Math.max(5,span*.21));
      }
    }
    if(!flags.includes('mouth_low_detail')) {
      const [mx,my]=p.mouth_center, half=Math.max(5,(p.mouth_right[0]-p.mouth_left[0])/2);
      const q=clamp((x-mx)/half,-1.1,1.1);
      const amplitude=(expression==='smile'?-5:2.7)*scale*(flags.includes('facial_hair_near_mouth')?.45:1);
      dy+=amplitude*(q*q-.18)*window(x,y,mx,my,half*1.8,Math.max(13,span*.43));
    }
    dy*=strength;
    const sy=y-dy, iy=Math.floor(sy), fy=sy-iy;
    if(Math.abs(dy)<1e-8 || iy<0 || iy+1>=height || source[(iy*width+x)*4+3]<240 || source[((iy+1)*width+x)*4+3]<240) continue;
    for(let c=0;c<3;c++) out[dst+c]=Math.round(source[(iy*width+x)*4+c]*(1-fy)+source[((iy+1)*width+x)*4+c]*fy);
  }
  return out;
}

export function gradeCharacter(pixels, background, effects) {
  const time=backgroundTime(background), profile=background?.lighting_profile||'outdoor';
  gradePixels(pixels,time,profile);
  gradeAtmosphere(pixels,backgroundPalette(background,effects),time,profile);
  return pixels;
}

export function spriteURL(asset,apiBase='') {
  return /^\/characters\/[a-z_]+_\d{3}-[a-f0-9]{64}\.png$/.test(asset?.url||'') ? apiBase+asset.url : null;
}

export class SurfaceCache {
  constructor(limit=12) {this.limit=limit;this.entries=new Map();}
  get(key) {if(!this.entries.has(key))return undefined;const value=this.entries.get(key);this.entries.delete(key);this.entries.set(key,value);return value;}
  set(key,value) {this.entries.delete(key);this.entries.set(key,value);while(this.entries.size>this.limit)this.entries.delete(this.entries.keys().next().value);return value;}
}
