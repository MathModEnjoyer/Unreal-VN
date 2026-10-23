import test from "node:test";
import assert from "node:assert/strict";
import { backingSize, backgroundTime, backgroundPalette, gradePixels, gradeAtmosphere } from "./backgroundProcessing.js";

const sample = new Uint8ClampedArray([230, 220, 190, 255, 110, 160, 210, 128, 15, 35, 10, 255]);
test("day preserves bytes; all variants preserve alpha and pixel count", () => {
  assert.deepEqual(gradePixels(sample.slice(), "day"), sample);
  for (const time of ["morning", "evening", "night"]) {
    const output = gradePixels(sample.slice(), time);
    assert.equal(output.length, sample.length);
    assert.notDeepEqual(output, sample);
    for (let index = 3; index < sample.length; index += 4) assert.equal(output[index], sample[index]);
  }
});
test("morning is warmer; night is darker; evening differs from night", () => {
  const morning = gradePixels(sample.slice(), "morning");
  const evening = gradePixels(sample.slice(), "evening");
  const night = gradePixels(sample.slice(), "night");
  assert.ok(morning[0] - morning[2] > sample[0] - sample[2]);
  const brightness = (bytes) => bytes.reduce((sum, value, index) => sum + (index % 4 === 3 ? 0 : value), 0);
  assert.ok(brightness(night) < brightness(evening));
  assert.ok(brightness(evening) < brightness(sample));
  assert.notDeepEqual(gradePixels(sample.slice(), "night", "windowed"), night);
});
test("fixed lighting never changes bytes, including legacy enclosed/space profiles", () => {
  for (const profile of ["fixed", "enclosed", "space"]) {
    for (const time of ["morning", "day", "evening", "night"]) {
      assert.deepEqual(gradePixels(sample.slice(), time, profile), sample);
    }
  }
  assert.equal(backgroundTime({time_of_day: "night", time_mode: "fixed"}), "neutral");
  assert.equal(backgroundTime({time_of_day: "night", is_fallback: true}), "neutral");
  assert.equal(backgroundTime({time_of_day: "night", lighting_profile: "space"}), "neutral");
  assert.equal(backgroundTime({asset_id: "metro_car", time_of_day: "night", lighting_profile: "windowed"}), "neutral");
  assert.equal(backgroundTime({time_of_day: "morning", time_mode: "windowed"}), "morning");
});
test("unknown time is neutral and unknown profile falls back", () => {
  assert.deepEqual(gradePixels(sample.slice(), "bad"), sample);
  assert.deepEqual(gradePixels(sample.slice(), "night", "bad"), gradePixels(sample.slice(), "night", "outdoor"));
});
test("adaptive backing size covers mobile, scales desktop, caps allocation", () => {
  assert.deepEqual(backingSize(1280, 720, 1920, 1080), { width: 1920, height: 1080 });
  const phone = backingSize(1280, 720, 390, 844, 3);
  assert.ok(phone.width <= 1920 && phone.height <= 1080);
  assert.ok(phone.width / phone.height > 1.77 && phone.width / phone.height < 1.79);
  assert.deepEqual(backingSize(1280, 720, 7680, 4320, 2), { width: 1920, height: 1080 });
});

test("weather palettes preserve alpha, mute colors and distinguish rain from storm", () => {
  const palette = type => ({strength:1,weather:{type,intensity:1}});
  const brightness = bytes => bytes[0]+bytes[1]+bytes[2];
  const rain = gradeAtmosphere(sample.slice(), palette('rain'));
  const storm = gradeAtmosphere(sample.slice(), palette('storm'));
  assert.ok(brightness(storm) < brightness(rain));
  assert.ok(brightness(rain) < brightness(sample));
  for (const type of ['rain','storm','snow','fog','ash']) {
    const output = gradeAtmosphere(sample.slice(), palette(type));
    assert.equal(output.length,sample.length);
    for (let i=3;i<sample.length;i+=4) assert.equal(output[i],sample[i]);
    assert.notDeepEqual(output,sample);
  }
  assert.deepEqual(gradeAtmosphere(sample.slice(), palette('unknown')),sample);
  assert.deepEqual(gradeAtmosphere(sample.slice(), {strength:1,weather:{type:'rain',intensity:NaN}}),sample);
  assert.deepEqual(gradeAtmosphere(sample.slice(), null),sample);
});

test("night fog stays dark; window light is subtle; fixed backgrounds reject grading", () => {
  const full = {strength:1,weather:{type:'fog',intensity:1}};
  const night = gradePixels(sample.slice(),'night');
  const fog = gradeAtmosphere(night.slice(),full,'night');
  assert.ok(fog[0]+fog[1]+fog[2] <= night[0]+night[1]+night[2]+10);
  const black = gradeAtmosphere(new Uint8ClampedArray([0,0,0,255]),full,'night');
  assert.ok(black[0]+black[1]+black[2] < 12);
  const window = gradeAtmosphere(sample.slice(),{...full,strength:.22});
  const outside = gradeAtmosphere(sample.slice(),full);
  assert.ok(Math.abs(window[0]-sample[0]) < Math.abs(outside[0]-sample[0]));
  for (const profile of ['fixed','enclosed','space','alien']) {
    assert.deepEqual(gradeAtmosphere(sample.slice(),full,'night',profile),sample);
  }
  for (const background of [{is_fallback:true}, {time_mode:'fixed'}, {effect_profile:{setting:'sealed'}}, {}]) {
    assert.equal(backgroundPalette(background,{grading:full}),null);
  }
  assert.deepEqual(backgroundPalette({effect_profile:{setting:'open'}},{grading:full}),full);
});
