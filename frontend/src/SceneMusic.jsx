import { useEffect, useRef, useState } from 'react';
import { MusicPlayer, selectedMusic, stableMusic } from './musicPlayer.js';
import { AUDIO_GESTURE, usePreferences } from './Preferences.jsx';

export default function SceneMusic({ music, apiBase = '', active }) {
  const { preferences, setAudioStatus } = usePreferences();
  const player = useRef(null);
  const [blocked, setBlocked] = useState(false), [loading, setLoading] = useState(false);
  const [track, setTrack] = useState(() => selectedMusic(music));
  const url = track ? `${apiBase}${track.url}` : null;
  const latest = useRef(null); latest.current = { url, active };
  useEffect(() => { setTrack(previous => stableMusic(music, previous)); }, [music]);
  useEffect(() => {
    player.current = new MusicPlayer({ onBlocked: setBlocked, onLoading: setLoading });
    const gesture = ({ detail: p }) => {
      setBlocked(false);
      player.current?.update({ ...latest.current, enabled: p.music, volume: p.musicVolume });
    };
    window.addEventListener(AUDIO_GESTURE, gesture);
    return () => { window.removeEventListener(AUDIO_GESTURE, gesture); player.current?.dispose(); player.current = null; };
  }, [apiBase]);
  useEffect(() => {
    player.current?.update({ url, enabled: preferences.music, active, volume: preferences.musicVolume });
  }, [url, preferences.music, active, preferences.musicVolume, apiBase]);
  useEffect(() => {
    setAudioStatus(previous => ({ ...previous, musicBlocked: blocked,
      music: blocked ? 'Браузер ожидает разрешения на воспроизведение' : loading ? 'Загрузка музыки…' :
        track ? [track.title, track.artist].filter(Boolean).join(' · ') : 'Пауза в музыке' }));
  }, [blocked, loading, track, setAudioStatus]);
  return null;
}
