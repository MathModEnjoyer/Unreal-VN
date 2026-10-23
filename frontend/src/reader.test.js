import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sceneBlocks, paginateBlocks, pageAt, speakerColor, interactiveTarget } from './reader.js';

test('ordered speech, thoughts and legacy prose remain readable without duplicate quotes', () => {
  const blocks = [{ type: 'narration', text: 'Тихо.' }, { type: 'dialogue', speaker_id: 'c2', text: 'Кто здесь?' }, { type: 'thought', text: 'Кажется, я не один.' }];
  assert.equal(sceneBlocks({ blocks }, [{ id: 'c2', name: 'Анна' }])[1].speaker, 'Анна');
  assert.equal(sceneBlocks({ narration: 'Привет!', dialogue: 'Привет!', speaker: 'Анна' }).length, 1);
  assert.equal(sceneBlocks({ narration: 'Тихо.', dialogue: 'Привет!', speaker: 'Анна' }).length, 2);
  assert.notEqual(speakerColor('c2'), speakerColor('c3'));
  assert.equal(speakerColor('c2'), speakerColor('c2'));
});
test('long paragraphs and even a single long word fit bounded pages without losing text', () => {
  for (const text of ['Первый абзац.\n\nВторой абзац с длинным продолжением. '.repeat(8), 'абвгдеёжз'.repeat(40)]) {
    const pages = paginateBlocks([{ type: 'narration', text }], 30, 3, t => t.length);
    assert.ok(pages.length > 1);
    assert.equal(pages.map(p => p.text).join('').replace(/\s/gu, ''), text.replace(/\s/gu, ''));
    for (const p of pages) assert.ok(p.text.replace(/\s/gu, '').length <= 90);
  }
});
test('changing font/viewport preserves the original block and character position', () => {
  const blocks = [{ type: 'narration', text: 'Слова о дороге и корабле. '.repeat(20) }, { type: 'dialogue', text: 'Привет', speaker_id: 'c2' }];
  const wide = paginateBlocks(blocks, 80, 4, t => t.length);
  const anchor = { block: wide[2].blockIndex, offset: wide[2].start };
  const narrow = paginateBlocks(blocks, 20, 2, t => t.length);
  const page = narrow[pageAt(narrow, anchor)];
  assert.equal(page.blockIndex, anchor.block);
  assert.ok(page.start <= anchor.offset && page.end > anchor.offset);
  assert.equal(pageAt([], { block: 0, offset: 0 }), 0);
});
test('controls and notebook clicks are excluded from global advance', () => {
  assert.equal(interactiveTarget({ closest: selector => selector.includes('input') ? {} : null }), true);
  assert.equal(interactiveTarget({ closest: () => null }), false);
  assert.equal(interactiveTarget(null), false);
});
