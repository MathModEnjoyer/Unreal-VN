import { useEffect, useRef, useState } from 'react';
import { ambienceTargets } from './ambiencePolicy.js';
import { AUDIO_GESTURE, usePreferences } from './Preferences.jsx';

export default function SceneAmbience({ effects, apiBase = '', active }) {
  const { preferences: p, setAudioStatus } = usePreferences();
  const tracks = useRef(null), current = useRef(null), latestTargets = useRef({});
  const [blocked, setBlocked] = useState(false);
  current.current = { effects, active };
  const targets = ambienceTargets(effects, p.ambience, active);
  for (const key of Object.keys(targets)) targets[key] *= p.ambienceVolume;
  latestTargets.current = targets;
  const signature = JSON.stringify(targets);
  useEffect(() => {
    tracks.current = Object.fromEntries(['rain', 'storm'].map(id => {
      const audio = new Audio(`${apiBase}/ambience/${id}.ogg`);
      audio.loop = true; audio.preload = 'none'; audio.volume = 0;
      return [id, audio];
    }));
    const gesture = ({ detail }) => {
      const wanted = ambienceTargets(current.current.effects, detail.ambience, current.current.active);
      Object.keys(wanted).forEach(id => { wanted[id] *= detail.ambienceVolume; });
      latestTargets.current = wanted;
      for (const [id, audio] of Object.entries(tracks.current || {})) {
        if (wanted[id] > 0) audio.play().then(() => {
          if (!latestTargets.current[id]) audio.pause(); else setBlocked(false);
        }).catch(() => setBlocked(true));
        else audio.pause();
      }
    };
    window.addEventListener(AUDIO_GESTURE, gesture);
    return () => {
      window.removeEventListener(AUDIO_GESTURE, gesture);
      Object.values(tracks.current || {}).forEach(audio => { audio.pause(); audio.removeAttribute('src'); audio.load(); });
      tracks.current = null;
    };
  }, [apiBase]);
  useEffect(() => {
    const values = JSON.parse(signature), audioTracks = tracks.current;
    if (!audioTracks) return;
    let cancelled = false;
    const from = Object.fromEntries(Object.entries(audioTracks).map(([id, audio]) => [id, audio.volume]));
    for (const [id, audio] of Object.entries(audioTracks)) {
      if (values[id] > 0 && audio.paused) audio.play().then(() => {
        if (!latestTargets.current[id]) audio.pause(); else if (!cancelled) setBlocked(false);
      }).catch(() => { if (!cancelled) setBlocked(true); });
    }
    const start = performance.now();
    const timer = setInterval(() => {
      const t = Math.min(1, (performance.now() - start) / 2400);
      for (const [id, audio] of Object.entries(audioTracks)) {
        audio.volume = Math.max(0, Math.min(1, from[id] + (values[id] - from[id]) * t));
        if (t === 1 && values[id] === 0) audio.pause();
      }
      if (t === 1) clearInterval(timer);
    }, 50);
    return () => { cancelled = true; clearInterval(timer); };
  }, [signature, apiBase]);
  useEffect(() => { setAudioStatus(old => ({ ...old, ambienceBlocked: blocked })); }, [blocked, setAudioStatus]);
  return null;
}
