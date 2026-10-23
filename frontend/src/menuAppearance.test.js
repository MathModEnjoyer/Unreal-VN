import test from 'node:test';
import assert from 'node:assert/strict';
import { localMenuTime, chooseMenuBackground } from './menuAppearance.js';
import { backgroundTime, gradePixels } from './backgroundProcessing.js';

test('menu follows device-local hours at every day/night boundary', () => {
  for (const [hour, minute, expected] of [
    [0, 0, 'night'], [4, 59, 'night'], [5, 0, 'morning'], [10, 59, 'morning'],
    [11, 0, 'day'], [16, 59, 'day'], [17, 0, 'evening'], [21, 59, 'evening'], [22, 0, 'night'],
  ]) {
    assert.equal(localMenuTime(new Date(2026, 9, 21, hour, minute)), expected);
  }
});

test('random menu selection excludes fixed lighting, fallbacks and external sources', () => {
  const outdoor = { asset_id:'forest_path', url:'/backgrounds/forest_path__day.webp', time_mode:'outdoor', lighting_profile:'outdoor' };
  const windowed = { asset_id:'bedroom', url:'/backgrounds/bedroom__day.webp', time_mode:'windowed', lighting_profile:'windowed' };
  const invalid = [null, { ...outdoor, time_mode:'fixed' }, { ...outdoor, is_fallback:true },
    { ...outdoor, url:'https://example.org/forest_path__day.webp' }, { ...outdoor, lighting_profile:'enclosed' }];
  assert.equal(chooseMenuBackground([...invalid, outdoor, windowed], () => 0), outdoor);
  assert.equal(chooseMenuBackground([...invalid, outdoor, windowed], () => .999), windowed);
  assert.equal(chooseMenuBackground(invalid).asset_id, 'forest_path');
  assert.equal(chooseMenuBackground(null).asset_id, 'forest_path');
});

test('all menu times use the existing actual pixel transforms, preserving source and alpha', () => {
  const selected = chooseMenuBackground([]);
  const original = new Uint8ClampedArray([180, 160, 120, 200]);
  const results = ['morning', 'day', 'evening', 'night'].map(time => {
    const item = { ...selected, time_of_day: time };
    const pixels = original.slice();
    gradePixels(pixels, backgroundTime(item), item.lighting_profile);
    assert.equal(pixels[3], 200);
    return pixels.join(',');
  });
  assert.equal(new Set(results).size, 4);
  assert.deepEqual([...original], [180, 160, 120, 200]);
});
