import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPremises } from './premiseSuggestions.js';

test('premises preserve server errors and allow an explicit retry', async () => {
  const original = globalThis.fetch;
  const signal = new AbortController().signal;
  let requests = 0;
  globalThis.fetch = async (url, options) => {
    requests++;
    assert.equal(url, '/story/premises');
    assert.ok(options.signal);
    if (requests === 1) return { ok: false, json: async () => ({detail:{message:'Проверь ключ DeepSeek'}}) };
    return { ok:true, json:async () => ({premises:[{genre:'Детектив',text:'Письмо из будущего'}]}) };
  };
  try {
    await assert.rejects(loadPremises(signal), /Проверь ключ DeepSeek/);
    assert.equal(requests, 1);
    assert.equal((await loadPremises(signal))[0].text, 'Письмо из будущего');
    assert.equal(requests, 2);
  } finally { globalThis.fetch = original; }
});

test('empty or malformed suggestions become visible errors instead of disappearing', async () => {
  const original = globalThis.fetch;
  try {
    for (const data of [null, {}, {premises:[]}, {premises:[{}, {text:' '}, {text:12}]}]) {
      globalThis.fetch = async () => ({ ok:true, json:async () => data });
      await assert.rejects(loadPremises(), /не вернула завязки/);
    }
    globalThis.fetch = async () => ({ok:false, json:async () => { throw new Error('not JSON'); }});
    await assert.rejects(loadPremises(), /Не удалось получить завязки/);
  } finally { globalThis.fetch = original; }
});
