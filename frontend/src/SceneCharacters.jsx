import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { backgroundTime, backgroundPalette } from './backgroundProcessing.js';
import { deformFace, gradeCharacter, spriteURL, SurfaceCache } from './characterProcessing.js';
import { characterSlots, CHARACTER_FADE_MS } from './characterStage.js';

import { paintPlaceholder } from './placeholderCharacter.js';

const EMPTY=[];
const originals=new SurfaceCache(3), surfaces=new SurfaceCache(8);
function loadOriginal(url) {
  const cached=originals.get(url);if(cached)return cached;
  const pending=new Promise((resolve,reject)=>{
    const image=new Image();image.crossOrigin='anonymous';image.decoding='async';
    image.onload=()=>resolve(image);image.onerror=()=>reject(new Error('Sprite unavailable'));image.src=url;
  });
  originals.set(url,pending);
  pending.catch(()=>{if(originals.entries.get(url)===pending)originals.entries.delete(url);});
  return pending;
}

async function renderSprite(asset,expression,background,effects,url) {
  const key=JSON.stringify([url,expression,backgroundTime(background),background?.lighting_profile,
    backgroundPalette(background,effects),asset.face]);
  const cached=surfaces.get(key);if(cached)return cached;
  const image=asset.is_fallback ? paintPlaceholder() : await loadOriginal(url);
  const [width,height]=asset.size||[];
  if((image.naturalWidth||image.width)!==width||(image.naturalHeight||image.height)!==height||width>1024||height>1536)throw new Error('Unexpected sprite size');
  const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
  const ctx=canvas.getContext('2d',{willReadFrequently:true});if(!ctx)throw new Error('Canvas unavailable');
  ctx.drawImage(image,0,0);
  const frame=ctx.getImageData(0,0,width,height);
  const pixels=deformFace(frame.data,width,height,asset.face,expression);
  gradeCharacter(pixels,background,effects);frame.data.set(pixels);ctx.putImageData(frame,0,0);
  const bounds=asset.bounds;
  if(!Array.isArray(bounds)||bounds.length!==4||!bounds.every(Number.isFinite)||
      bounds[0]<0||bounds[1]<0||bounds[2]>width||bounds[3]>height||bounds[2]<=bounds[0]||bounds[3]<=bounds[1])throw new Error('Invalid sprite crop');
  const result=document.createElement('canvas');result.width=bounds[2]-bounds[0];result.height=bounds[3]-bounds[1];
  result.getContext('2d').drawImage(canvas,...bounds.slice(0,2),result.width,result.height,0,0,result.width,result.height);
  return surfaces.set(key,result);
}

function Character({ character, background,effects,apiBase,leaving=false,center,width,top,height,crowd,portraitHeight,onArtFailure }) {
  const ref=useRef(null),display=useRef(null),[ready,setReady]=useState(false);
  const [failed,setFailed]=useState(null);
  const original=character.sprite;
  const asset=failed===original.asset_id ? {is_fallback:true,size:[512,768],bounds:[0,0,512,768],face:null} : original;
  const url=asset.is_fallback ? `placeholder:${character.id}` : spriteURL(asset,apiBase);
  const report=useRef(onArtFailure);report.current=onArtFailure;
  const expression=character.expression||'base';
  const eyes=asset.face?.points_px;
  const faceX=Number.isFinite(eyes?.eye_left?.[0])&&Number.isFinite(eyes?.eye_right?.[0])?
    (eyes.eye_left[0]+eyes.eye_right[0])/2:(asset.bounds[0]+asset.bounds[2])/2;
  const anchor=Math.max(0,Math.min(100,100*(faceX-asset.bounds[0])/(asset.bounds[2]-asset.bounds[0])));
  const palette=JSON.stringify([backgroundTime(background),background?.lighting_profile,backgroundPalette(background,effects)]);
  const assetKey=JSON.stringify([asset.face,asset.size,asset.bounds]);
  const inputs=useRef(null);inputs.current={asset,background,effects};
  useEffect(()=>{
    const {asset,background,effects}=inputs.current;
    let cancelled=false,frame=null;
    const media=window.matchMedia('(prefers-reduced-motion: reduce)');
    if(display.current?.url!==url){display.current=null;setReady(false);}
    let finish=()=>{};
    if(url)renderSprite(asset,expression,background,effects,url).then(target=>{
      if(cancelled||!ref.current)return;
      const previous=display.current;
      const from=previous?.surface;
      const surface=document.createElement('canvas');surface.width=target.width;surface.height=target.height;
      const ctx=surface.getContext('2d'),output=ref.current;
      output.width=target.width;output.height=target.height;
      const draw=output.getContext('2d');if(!ctx||!draw)return;
      display.current={url,surface,palette};
      const duration=previous?.palette!==palette?2400:350;
      const started=performance.now();let complete=!from||media.matches||document.hidden;
      const render=now=>{
        if(cancelled)return;
        const t=complete?1:Math.min(1,(now-started)/duration),mix=t*t*(3-2*t);
        ctx.clearRect(0,0,surface.width,surface.height);
        ctx.globalCompositeOperation='source-over';ctx.globalAlpha=1-mix;
        if(from)ctx.drawImage(from,0,0,surface.width,surface.height);
        // Add premultiplied weights so transparency does not pulse mid-fade.
        ctx.globalCompositeOperation='lighter';ctx.globalAlpha=mix;ctx.drawImage(target,0,0);
        ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
        draw.clearRect(0,0,output.width,output.height);draw.drawImage(surface,0,0);setReady(true);
        if(t<1)frame=requestAnimationFrame(render);else frame=null;
      };
      finish=()=>{if(media.matches||document.hidden){if(frame!==null)cancelAnimationFrame(frame);complete=true;render(performance.now());}};
      render(started);
    }).catch(()=>{if(!cancelled){
      setReady(false);
      report.current?.(character.id,asset.is_fallback?'canvas_failed':'image_load_failed');
      if(!asset.is_fallback)setFailed(original.asset_id);
    }});
    const settle=()=>finish();media.addEventListener('change',settle);document.addEventListener('visibilitychange',settle);
    return()=>{cancelled=true;if(frame!==null)cancelAnimationFrame(frame);media.removeEventListener('change',settle);document.removeEventListener('visibilitychange',settle);};
  },[url,expression,palette,assetKey,character.id,original.asset_id]);
  return <div className={`stage-character ${character.speaking?'stage-character--speaking':''} ${leaving?'stage-character--leaving':''}`} data-character-id={character.id} data-expression={expression} data-ready={ready}
    data-frame={crowd?'portrait':'body'}
    style={{'--character-x':`${center}%`,'--character-width':`${width}%`,'--character-y':`${top}px`,
      '--character-height':`${height}px`,'--character-portrait-height':`${portraitHeight}px`,'--character-portrait-anchor':`-${anchor}%`}}>
    <canvas ref={ref} role="img" aria-label={character.name} />
  </div>;
}

export default function SceneCharacters({characters=EMPTY,background,effects,apiBase='',onArtFailure,activeSpeakerId}) {
  const stageRef=useRef(null);
  const [space,setSpace]=useState({width:1000,height:500,fullHeight:900});
  const [shown,setShown]=useState(()=>characterSlots(characters,[],space));
  useLayoutEffect(()=>{
    const layer=stageRef.current,stage=layer.closest('.stage'),dialogue=stage.querySelector('.dialogue-window');

    const measure=()=>{
      const bounds=layer.getBoundingClientRect();
      const top=0;
      const next={width:Math.max(1,bounds.width),top,height:Math.max(80,(dialogue?.getBoundingClientRect().top??bounds.bottom)-bounds.top-top-12),fullHeight:Math.max(1,bounds.height-top)};
      setSpace(old=>Object.keys(next).every(key=>Number.isFinite(old[key])&&Math.abs(old[key]-next[key])<.5)?old:next);
    };
    measure();
    const observer=new ResizeObserver(measure);observer.observe(layer);observer.observe(stage);
    if(dialogue)observer.observe(dialogue);
    return()=>observer.disconnect();
  },[]);
  useLayoutEffect(()=>{
    setShown(previous=>characterSlots(characters,previous,space));
    const timer=setTimeout(()=>setShown(previous=>previous.filter(item=>!item.leaving)),CHARACTER_FADE_MS);
    return()=>clearTimeout(timer);
  },[characters,space]);
  return <div ref={stageRef} className="scene-characters" data-count={characters.length} style={{'--character-fade':`${CHARACTER_FADE_MS}ms`}}>
    {shown.map(({character,leaving,...slot})=><Character key={character.id} character={{...character,speaking:activeSpeakerId === character.id}} leaving={leaving} {...slot}
      background={background} effects={effects} apiBase={apiBase} onArtFailure={onArtFailure} />)}
  </div>;
}
