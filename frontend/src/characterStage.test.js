import test from 'node:test';
import assert from 'node:assert/strict';
import {characterSlots} from './characterStage.js';

const cast = count => Array.from({length: count}, (_, index) => ({id: `c${index + 1}`}));

test('1–12 cast slots stay within mobile/desktop stages without overlapping or hiding faces', () => {
  for (const space of [{width:382,top:58,height:346,fullHeight:792},{width:1698,top:94,height:524,fullHeight:1206}]) {
   for (const count of [1, 2, 3, 4, 6, 8, 12]) {
    const slots = characterSlots(cast(count), [], space);
    assert.equal(slots.length, count);
    slots.forEach((slot, index) => {
      assert.ok(slot.center - slot.width / 2 >= -1e-9);
      assert.ok(slot.center + slot.width / 2 <= 100 + 1e-9);
      assert.ok(slot.top >= space.top);
      if (slot.crowd) assert.ok(slot.top + slot.height <= space.top + space.height + 1e-9);
      for (const other of slots.slice(0,index)) {
        const separateX = Math.abs(other.center-slot.center) >= (other.width+slot.width)/2-1e-9;
        const separateY = Math.abs(other.top-slot.top) >= (other.height+slot.height)/2-1e-9;
        assert.ok(separateX||separateY);
      }
    });
   }
  }
  assert.deepEqual(characterSlots([]), []);
});

test('departures retain their old geometry without occupying new cast slots', () => {
  const old = characterSlots(cast(8));
  const next = characterSlots(cast(2), old);
  assert.equal(next.filter(slot => !slot.leaving).length, 2);
  assert.deepEqual(next.filter(slot => !slot.leaving).map(slot => [slot.center, slot.width]), [[25, 50], [75, 50]]);
  assert.deepEqual(next.slice(2), old.slice(2).map(slot => ({...slot, leaving: true})));
  const empty = characterSlots([], next);
  assert.equal(empty.length, 8);
  assert.ok(empty.every(slot => slot.leaving));
});

test('rapid reentry cancels departure and refreshes the same ID without duplicate figures', () => {
  const old = characterSlots(cast(3));
  const departing = characterSlots(cast(1), old);
  const updated = {...cast(3)[1], expression: 'smile', speaking: true};
  const next = characterSlots([updated], departing);
  assert.equal(new Set(next.map(slot => slot.character.id)).size, 3);
  const reentered = next.find(slot => slot.character.id === updated.id);
  assert.equal(reentered.leaving, false);
  assert.equal(reentered.character, updated);
  assert.equal(reentered.center, 50);
  assert.equal(reentered.width, 100);
});
