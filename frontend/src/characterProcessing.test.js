import test from 'node:test';
import assert from 'node:assert/strict';
import {deformFace,gradeCharacter,validFace,spriteURL,SurfaceCache} from './characterProcessing.js';
const face={size:[100,130],points_px:{eye_left:[30,40],eye_right:[70,40],brow_left:[30,25],brow_right:[70,25],mouth_left:[38,70],mouth_center:[50,70],mouth_right:[62,70]},regions_px:{working_face:[10,10,90,100]},review_flags:[]};
const source=Uint8ClampedArray.from({length:100*130*4},(_,i)=>i%4===3?254:(Math.floor(i/400)*7+i%4*15)%256);
test('face warps preserve alpha, silhouette, outside ROI and source; expressions differ',()=>{
 const before=source.slice(),smile=deformFace(source,100,130,face,'smile'),tension=deformFace(source,100,130,face,'tension');
 assert.notDeepEqual(smile,tension);assert.notDeepEqual(smile,source);assert.deepEqual(source,before);
 for(let y=0;y<130;y++)for(let x=0;x<100;x++)for(let k=0;k<4;k++){
  const i=(y*100+x)*4+k;
  if(k===3||x<10||x>=90||y<10||y>=100){assert.equal(smile[i],source[i]);assert.equal(tension[i],source[i]);}
 }
 assert.deepEqual(deformFace(source,100,130,face,'base'),source);
 assert.deepEqual(deformFace(source,100,130,face,'surprise'),source);
 assert.deepEqual(deformFace(source,100,130,null,'smile'),source);
 assert.equal(validFace({...face,size:[9,9]},100,130),false);
});
test('sprites use the approved indoor/time/weather policy and preserve alpha',()=>{
 const indoor={time_mode:'fixed',time_of_day:'night',lighting_profile:'fixed',effect_profile:{setting:'enclosed'}};
 const rain={grading:{strength:1,weather:{type:'storm',intensity:1}}};
 assert.deepEqual(gradeCharacter(source.slice(),indoor,rain),source);
 const night=gradeCharacter(source.slice(),{time_of_day:'night',time_mode:'cycle',lighting_profile:'outdoor',effect_profile:{setting:'open'}},rain);
 assert.notDeepEqual(night,source);for(let i=3;i<source.length;i+=4)assert.equal(night[i],source[i]);
});
test('surface cache is bounded LRU; sprite URLs are hash-addressed local files',()=>{
 const cache=new SurfaceCache(2);cache.set('a',1);cache.set('b',2);cache.get('a');cache.set('c',3);
 assert.equal(cache.get('b'),undefined);assert.equal(cache.entries.size,2);
 const url='/characters/man_casual_001-'+'a'.repeat(64)+'.png';assert.equal(spriteURL({url},'http://localhost'), 'http://localhost'+url);
 for(const url of ['https://evil/a.png','/characters/../../secret','/characters/a.png'])assert.equal(spriteURL({url}),null);
});
