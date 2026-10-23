import test from "node:test";
import assert from "node:assert/strict";
import { ambienceTargets } from "./ambiencePolicy.js";

test("only approved rain/storm effects have ambience", () => {
  for (const weather of [null, { type: "snow" }, { type: "fog" }]) {
    assert.deepEqual(ambienceTargets({ weather }, true), { rain: 0, storm: 0 });
  }
  assert(ambienceTargets({ weather: { type: "rain", intensity: 0.5 } }, true).rain > 0);
  assert.equal(ambienceTargets({ weather: { type: "storm" } }, true).rain, 0);
  assert(ambienceTargets({ weather: { type: "storm" } }, true).storm > 0);
});
test("mute/inactive state silences all tracks", () => {
  assert.deepEqual(ambienceTargets({ weather: { type: "rain" } }, false), { rain: 0, storm: 0 });
  assert.deepEqual(ambienceTargets({ weather: { type: "storm" } }, true, false), { rain: 0, storm: 0 });
});
