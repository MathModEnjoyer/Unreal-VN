import test from "node:test";
import assert from "node:assert/strict";
import { MusicPlayer, musicVolume, selectedMusic, stableMusic } from "./musicPlayer.js";

const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  let time = 0, timer = null;
  const audios = [], blocked = [];
  const player = new MusicPlayer({
    now: () => time,
    schedule: fn => { timer = fn; return 1; },
    cancel: () => { timer = null; },
    onBlocked: value => blocked.push(value),
    createAudio: url => {
      const audio = { url, paused: true, currentTime: 0, volume: 0, calls: 0,
        play() { this.calls++; this.paused = false; return Promise.resolve(); },
        pause() { this.paused = true; },
        removeAttribute() { this.released = true; }, load() {} };
      audios.push(audio); return audio;
    },
  });
  return { player, audios, blocked, advance(ms) { time += ms; timer?.(); },
    update(url, extra = {}) { player.update({ url, enabled: true, active: true, volume: .5, ...extra }); } };
}

test("small score changes retain suitable music; mood changes and silence do not", () => {
  const a = { id: "a", url: "/music/a.ogg", score: .7, moods: ["calm"] };
  const b = { id: "b", url: "/music/b.ogg", score: .8, moods: ["calm"] };
  const rec = { selected: "b", tracks: [b, a], query: { preferred_mood: "calm" } };
  assert.equal(stableMusic(rec, a).id, "a");
  assert.equal(stableMusic({ ...rec, query: { preferred_mood: "tense" } }, a).id, "b");
  assert.equal(stableMusic({ ...rec, silence: true }, a), null);
});

test("only server music URLs are playable", () => {
  const rec = url => ({ selected: "a", tracks: [{ id: "a", url }] });
  assert.equal(selectedMusic(rec("/music/calm.ogg")).id, "a");
  for (const url of ["https://host/file.mp3", "/music/../secret.mp3", "/music/a.mp3?x=1", "//host/a.mp3"]) {
    assert.equal(selectedMusic(rec(url)), null);
  }
  assert.equal(selectedMusic({ ...rec("/music/a.mp3"), silence: true }), null);
  assert.equal(selectedMusic({ selected: "a", tracks: {} }), null);
  assert.equal(selectedMusic({ selected: "a", tracks: [null] }), null);
  assert.equal(musicVolume(4), 1);
  assert.equal(musicVolume(-1), 0);
  assert.equal(musicVolume(undefined), .35);
});

test("same track continues; a different track crossfades then releases outgoing audio", async () => {
  const h = harness(); h.update("a"); await settle(); h.advance(2500);
  const a = h.audios[0]; a.currentTime = 42;
  h.update("a"); await settle(); h.advance(2500);
  assert.equal(h.audios.length, 1); assert.equal(a.currentTime, 42);
  assert.equal(a.calls, 1); assert.equal(a.volume, .3);
  h.update("b"); await settle(); h.advance(1250);
  assert.equal(a.volume, .15); assert.equal(h.audios[1].volume, .15);
  h.advance(1250);
  assert.ok(a.paused && a.released); assert.equal(h.player.voices.size, 1);
});

test("rapid scene changes keep at most two audio voices", async () => {
  const h = harness();
  for (let i = 0; i < 12; i++) {
    h.update(String(i)); await settle(); h.advance(200);
    assert.ok(h.player.voices.size <= 2);
    assert.ok(h.audios.filter(a => !a.released).length <= 2);
  }
  h.advance(2500); assert.equal(h.player.voices.size, 1);
});

test("mute and leaving the game pause immediately, retaining playback position", async () => {
  const h = harness(); h.update("a"); await settle(); h.advance(2500);
  h.audios[0].currentTime = 10;
  h.update("a", { active: false });
  assert.equal(h.audios[0].paused, true); assert.equal(h.audios[0].volume, 0);
  h.update("a"); assert.equal(h.audios[0].currentTime, 10);
  h.update("a", { enabled: false }); assert.equal(h.audios[0].paused, true);
  h.advance(10000); assert.equal(h.audios[0].volume, 0);
});

test("silence fades out; dispose releases all audio and timers", async () => {
  const h = harness(); h.update("a"); await settle(); h.advance(2500);
  h.update(null); h.advance(1250); assert.equal(h.audios[0].volume, .15);
  h.advance(1250); assert.equal(h.player.voices.size, 0);
  h.update("b"); h.player.dispose(); h.advance(5000);
  assert.equal(h.player.voices.size, 0);
  assert.ok(h.audios.every(a => a.paused && a.released));
});

test("autoplay failure is reported and can be retried with the same audio", async () => {
  const h = harness(); h.update("a", { enabled: false });
  const originalCreate = h.player.createAudio;
  h.player.createAudio = url => {
    const a = originalCreate(url);
    a.play = () => Promise.reject(Object.assign(new Error("blocked"), { name: "NotAllowedError" }));
    return a;
  };
  h.update("a"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.blocked.at(-1), true);
  h.audios[0].play = function () { this.paused = false; return Promise.resolve(); };
  h.update("a"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.blocked.at(-1), false);
  assert.equal(h.audios.length, 1);
});

test("outgoing music continues while the new file downloads, then crossfades", async () => {
  const h = harness(); h.update("a"); await settle(); h.advance(2500);
  const create = h.player.createAudio;
  let ready;
  h.player.createAudio = url => {
    const audio = create(url);
    audio.play = function () { this.paused = false; return new Promise(resolve => { ready = resolve; }); };
    return audio;
  };
  h.update("b"); h.advance(10000);
  assert.equal(h.audios[0].volume, .3);
  assert.equal(h.audios[1].volume, 0);
  ready(); await settle(); h.advance(1250);
  assert.equal(h.audios[0].volume, .15);
  assert.equal(h.audios[1].volume, .15);
  h.player.dispose();
});

test("muting a pending download cannot restart music; failed downloads can reload", async () => {
  const h = harness(); const create = h.player.createAudio;
  let ready;
  h.player.createAudio = url => {
    const audio = create(url);
    audio.play = () => new Promise(resolve => { ready = resolve; });
    return audio;
  };
  h.update("a"); h.update("a", { enabled: false });
  ready(); await settle(); h.advance(10000);
  assert.equal(h.audios[0].volume, 0);
  assert.equal(h.audios[0].paused, true);
  h.audios[0].play = () => Promise.reject(new Error("network"));
  h.update("a"); await settle(); assert.equal(h.blocked.at(-1), true);
  let reloads = 0;
  h.audios[0].load = () => { reloads++; };
  h.audios[0].play = function () { this.paused = false; return Promise.resolve(); };
  h.update("a"); await settle(); h.advance(2500);
  assert.equal(reloads, 1); assert.equal(h.audios[0].volume, .3);
  h.player.dispose();
});
