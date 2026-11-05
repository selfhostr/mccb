// Run: docker run --rm -v /home/x0/psttoimap:/w -w /w node:22-slim node --test tests/web/
import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.addEventListener ??= () => {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.Node = class {};
globalThis.document = { createTextNode: s => s, createElement: () => ({ remove() {}, append() {}, setAttribute() {}, addEventListener() {}, style: {}, classList: { add() {}, remove() {} } }), body: { append() {} }, getElementById: () => ({ append() {} }), querySelector: () => null };

const sleep = ms => new Promise(r => setTimeout(r, ms));
const puts = [];
const stored = new Set(); // chunks reçus (PUT terminés) côté serveur           // numéros de chunks PUT
let inflight = [];         // fetchs bloqués
const N = 12, CS = 10;

globalThis.fetch = (url, init = {}) => {
  const json = o => Promise.resolve(new Response(JSON.stringify(o), { status: 200, headers: { 'content-type': 'application/json' } }));
  const m = /\/chunks\/(\d+)$/.exec(url);
  if (init.method === 'PUT' && m) {
    puts.push(+m[1]);
    return new Promise((res, rej) => {
      const done = () => { stored.add(+m[1]); res(new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })); };
      const t = setTimeout(done, 40);
      init.signal?.addEventListener('abort', () => { clearTimeout(t); rej(new DOMException('aborted', 'AbortError')); });
    });
  }
  if (init.method === 'POST' && url.endsWith('/uploads')) return json({ id: 'u1', chunk_size: CS, received: [...stored] });
  if (init.method === 'GET') return json({ received: [...stored] });
  return json({ pst_id: 'p1' });
};

const up = await import('../../app/web/js/uploader.js');

test('pause puis resume : chaque chunk PUT une seule fois', async () => {
  const file = { name: 'a.pst', size: N * CS, lastModified: 1, slice: () => ({ arrayBuffer: async () => new ArrayBuffer(CS) }) };
  const t = up.addFile(file);
  await sleep(60);
  up.pause(t);
  const afterPause = puts.length;
  assert.ok(afterPause > 0 && afterPause < N);
  await sleep(10);
  up.resume(t);
  for (let i = 0; i < 100 && t.status !== 'done'; i++) await sleep(50);
  assert.equal(t.status, 'done');
  // chunks interrompus en vol (abort) peuvent être renvoyés une fois ; jamais plus
  const counts = {}; for (const n of puts) counts[n] = (counts[n] || 0) + 1;
  for (const [n, c] of Object.entries(counts)) assert.ok(c <= 2, `chunk ${n} envoyé ${c} fois`);
  assert.ok(puts.length <= N + 3, `trop de PUT: ${puts.length} pour ${N} chunks`);
  assert.equal(t.sentBytes, N * CS);
  assert.equal(t.received.size, N);
});
