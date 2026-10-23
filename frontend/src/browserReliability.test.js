import test from 'node:test';
import assert from 'node:assert/strict';
import { browserStorage } from './browserStorage.js';
import { apiFetch, apiError } from './api.js';

test('denied browser storage does not prevent reading or navigating', () => {
  const previous = globalThis.window;
  globalThis.window = Object.defineProperty({}, 'localStorage', { get() { throw new Error('denied'); } });
  try {
    assert.equal(browserStorage.getItem('session'), null);
    assert.doesNotThrow(() => browserStorage.setItem('session', 'test'));
    assert.doesNotThrow(() => browserStorage.removeItem('session'));
  } finally { globalThis.window = previous; }
});

test('full storage still allows an active story to continue', () => {
  const previous = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => 'old', setItem() { throw new Error('quota'); }, removeItem() {} } };
  try {
    assert.equal(browserStorage.getItem('session'), 'old');
    assert.doesNotThrow(() => browserStorage.setItem('session', 'new'));
  } finally { globalThis.window = previous; }
});

test('API errors preserve readable string and structured messages', () => {
  assert.equal(apiError({detail:'История завершена'}, 'fallback'), 'История завершена');
  assert.equal(apiError({detail:{message:'Повторите позже'}}, 'fallback'), 'Повторите позже');
  assert.equal(apiError({detail:[]}, 'fallback'), 'fallback');
  assert.equal(apiError(null, 'fallback'), 'fallback');
});

test('API request bounds waiting, keeps body and releases timer on success/failure', async () => {
  const saved = { fetch:globalThis.fetch, setTimeout:globalThis.setTimeout, clearTimeout:globalThis.clearTimeout };
  let delay, cleared = 0, sent;
  globalThis.setTimeout = (callback, ms) => { delay = ms; return 123; };
  globalThis.clearTimeout = id => { assert.equal(id,123); cleared++; };
  globalThis.fetch = async (url, options) => { sent=options; return {ok:true}; };
  try {
    await apiFetch('/health');
    assert.equal(delay,30_000);
    await apiFetch('/story/scene',{method:'POST',body:'{"choice":"A"}'});
    assert.equal(delay,1_200_000);
    assert.equal(sent.body,'{"choice":"A"}');
    await apiFetch('/story/premises');
    assert.equal(delay,240_000);
    globalThis.fetch = async () => { throw new Error('offline'); };
    await assert.rejects(apiFetch('/health'), /offline/);
    assert.equal(cleared,4);
  } finally { Object.assign(globalThis,saved); }
});

test('already cancelled requests remain cancelled', async () => {
  const saved = globalThis.fetch;
  const cancel = new AbortController(); cancel.abort();
  globalThis.fetch = async (url, options) => {
    assert.equal(options.signal.aborted,true);
    throw new Error('cancelled');
  };
  try { await assert.rejects(apiFetch('/story/scene',{signal:cancel.signal}),/cancelled/); }
  finally { globalThis.fetch=saved; }
});
