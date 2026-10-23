export function selectedMusic(recommendation) {
  if (recommendation?.silence || !recommendation?.selected) return null;
  const track = Array.isArray(recommendation.tracks)
    ? recommendation.tracks.find(item => item?.id === recommendation.selected) : null;
  // Only server-validated music routes, never URLs from an imported scene.
  return track && /^\/music\/[a-z0-9_-]+\.(?:ogg|mp3)$/.test(track.url || "") ? track : null;
}

export function musicVolume(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(1, number)) : 0.35;
}

export function stableMusic(recommendation, previous) {
  const best = selectedMusic(recommendation);
  if (!best || !previous) return best;
  const retained = recommendation.tracks.find(item => item?.id === previous.id);
  const mood = recommendation.query?.preferred_mood;
  // Small score fluctuations between adjacent scenes should not restart the soundtrack.
  return retained && retained.url === previous.url && retained.moods?.includes(mood)
    && best.score - retained.score <= 0.12 ? retained : best;
}

export class MusicPlayer {
  constructor({ createAudio = url => new Audio(url), now = () => performance.now(),
    schedule = fn => setInterval(fn, 50), cancel = id => clearInterval(id),
    onBlocked = () => {}, onLoading = () => {} } = {}) {
    Object.assign(this, { createAudio, now, schedule, cancel, onBlocked, onLoading });
    this.voices = new Map();
    this.timer = null;
    this.current = null;
    this.playing = false;
    this.pending = new WeakMap();
    this.failed = new WeakSet();
    this.generation = 0;
  }

  clearTimer() {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
  }

  remove(url) {
    const audio = this.voices.get(url);
    if (!audio) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    this.voices.delete(url);
  }

  update({ url, enabled, active, volume }) {
    const generation = ++this.generation;
    this.clearTimer();
    this.current = url || null;
    this.playing = Boolean(enabled && active);
    if (!this.playing) {
      this.onLoading(false);
      // Muting or leaving the game stops playback; tab visibility is irrelevant.
      for (const [key, audio] of this.voices) {
        audio.volume = 0;
        audio.pause();
        if (key !== this.current) this.remove(key);
      }
      return;
    }
    if (url && !this.voices.has(url)) {
      // Keep at most one outgoing voice when choices change rapidly.
      const outgoing = [...this.voices].sort((a, b) => b[1].volume - a[1].volume);
      for (const [key] of outgoing.slice(1)) this.remove(key);
      const audio = this.createAudio(url);
      audio.loop = true;
      audio.preload = "none";
      audio.volume = 0;
      this.voices.set(url, audio);
    }
    const fade = () => {
      const from = new Map([...this.voices].map(([key, audio]) => [key, audio.volume]));
      const target = musicVolume(volume) * 0.6;
      const start = this.now();
      const tick = () => {
        const progress = Math.min(1, (this.now() - start) / 2500);
        for (const [key, audio] of this.voices) {
          const level = key === this.current ? target : 0;
          audio.volume = musicVolume(from.get(key) + (level - from.get(key)) * progress);
          if (progress === 1 && key !== this.current) this.remove(key);
        }
        if (progress === 1) this.clearTimer();
      };
      this.timer = this.schedule(tick);
    };
    const incoming = this.voices.get(url);
    let pending = incoming && this.pending.get(incoming);
    if (incoming?.paused && !pending) {
      if (this.failed.has(incoming)) {
        incoming.load();
        this.failed.delete(incoming);
      }
      try { pending = Promise.resolve(incoming.play()); }
      catch (error) { pending = Promise.reject(error); }
      this.pending.set(incoming, pending);
    }
    if (!pending) {
      this.onLoading(false);
      fade();
      return;
    }
    this.onLoading(true);
    const relevant = () => this.playing && this.current === url && this.voices.get(url) === incoming;
    pending.then(() => {
      if (this.pending.get(incoming) === pending) this.pending.delete(incoming);
      if (relevant()) {
        this.onBlocked(false);
        this.onLoading(false);
        // Keep outgoing music audible until the new download can actually play.
        if (generation === this.generation) fade();
      }
    }).catch(error => {
      if (this.pending.get(incoming) === pending) this.pending.delete(incoming);
      if (error?.name !== "AbortError" && error?.name !== "NotAllowedError") this.failed.add(incoming);
      if (relevant()) {
        this.onLoading(false);
        if (error?.name !== "AbortError") this.onBlocked(true);
      }
    });
  }

  dispose() {
    this.generation++;
    this.clearTimer();
    for (const key of [...this.voices.keys()]) this.remove(key);
    this.current = null;
    this.playing = false;
  }
}
