import { createContext, useContext, useState } from 'react';
import { browserStorage } from './browserStorage.js';

const Context = createContext(null);
export const AUDIO_GESTURE = 'story-engine-audio-gesture';
const read = (key, fallback) => browserStorage.getItem(key) ?? fallback;
export function PreferencesProvider({ children }) {
  const [preferences, setPreferences] = useState(() => ({
    music: read('story_engine_music', 'off') === 'on',
    musicVolume: Math.max(0, Math.min(1, Number(read('story_engine_music_volume', '.35')) || 0)),
    ambience: read('story_engine_ambience', 'off') === 'on',
    ambienceVolume: Math.max(0, Math.min(1, Number(read('story_engine_ambience_volume', '.7')) || 0)),
    fontSize: Math.max(16, Math.min(36, Number(read('story_engine_font_size', '24')) || 24)),
  }));
  const [audioStatus, setAudioStatus] = useState({});
  function update(key, value) {
    const next = { ...preferences, [key]: value };
    setPreferences(next);
    const names = { music: 'music', musicVolume: 'music_volume', ambience: 'ambience', ambienceVolume: 'ambience_volume', fontSize: 'font_size' };
    browserStorage.setItem(`story_engine_${names[key]}`, typeof value === 'boolean' ? value ? 'on' : 'off' : String(value));
    // Browser playback must start in the original click, including Retry.
    if (key !== 'fontSize') window.dispatchEvent(new CustomEvent(AUDIO_GESTURE, { detail: next }));
  }
  return <Context.Provider value={{ preferences, update, audioStatus, setAudioStatus }}>{children}</Context.Provider>;
}
export const usePreferences = () => useContext(Context);

export function PreferenceControls() {
  const { preferences: p, update, audioStatus } = usePreferences();
  return <section className="preference-controls" aria-label="Звук и текст">
    <h2>Звук и текст</h2>
    <p className="field__hint">Эти настройки сохраняются автоматически.</p>
    <label><input type="checkbox" checked={p.music} onChange={e => update('music', e.target.checked)} /> Музыка</label>
    <label>Громкость музыки
      <input aria-label="Громкость музыки" type="range" min="0" max="100" value={Math.round(p.musicVolume * 100)} onChange={e => update('musicVolume', Number(e.target.value) / 100)} />
    </label>
    <p className="field__hint">{audioStatus.music || 'Музыка включается во время игры'}</p>
    {audioStatus.musicBlocked && <button className="btn btn--ghost" onClick={() => update('music', true)}>Разрешить воспроизведение музыки</button>}
    <label><input type="checkbox" checked={p.ambience} onChange={e => update('ambience', e.target.checked)} /> Звуки дождя и грозы</label>
    <label>Громкость звуков
      <input aria-label="Громкость звуков" type="range" min="0" max="100" value={Math.round(p.ambienceVolume * 100)} onChange={e => update('ambienceVolume', Number(e.target.value) / 100)} />
    </label>
    {audioStatus.ambienceBlocked && <button className="btn btn--ghost" onClick={() => update('ambience', true)}>Разрешить воспроизведение звуков</button>}
    <a href="/music-credits" target="_blank" rel="noreferrer">Музыка и лицензии</a>
    <label>Размер игрового текста: {p.fontSize} px
      <input aria-label="Размер игрового текста" type="range" min="16" max="36" value={p.fontSize} onChange={e => update('fontSize', Number(e.target.value))} />
    </label>
    <p className="font-preview" style={{ fontSize: `${p.fontSize}px` }}>За окном медленно загораются огни.</p>
  </section>;
}
