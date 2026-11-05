// Global resumable upload manager (survives page navigation).
import { get, post, put, emit, on, ApiError } from './api.js';
import { sha256Hex } from './sha256.js';
import { toast } from './ui.js';

const LS = 'pstmig.uploads.v1';
const PARALLEL = 3, MAX_TRIES = 8;
export const tasks = new Map(); // key -> task
const sleep = ms => new Promise(r => setTimeout(r, ms));

export const fileKey = f => `${f.name}|${f.size}|${f.lastModified}`;
const loadLS = () => { try { return JSON.parse(localStorage.getItem(LS) || '{}'); } catch { return {}; } };
const saveLS = o => { try { localStorage.setItem(LS, JSON.stringify(o)); } catch {} };
const lsSet = t => { const o = loadLS(); o[t.key] = { key: t.key, id: t.id, filename: t.filename, size: t.size, lastModified: t.lastModified, ts: Date.now() }; saveLS(o); };
const lsDel = key => { const o = loadLS(); delete o[key]; saveLS(o); };

// Uploads known to the browser whose file is not (or no longer) selected
export function pendingResumables() {
  return Object.values(loadLS()).filter(r => !tasks.has(r.key)).sort((a, b) => b.ts - a.ts);
}
export function forgetResumable(key) { lsDel(key); emit('uploader'); }
export const hasActiveUploads = () => [...tasks.values()].some(t => t.status === 'uploading' || t.status === 'hashing' || t.status === 'starting');

const changed = () => emit('uploader');

export function addFile(file, { onValid } = {}) {
  const key = fileKey(file);
  const old = tasks.get(key);
  if (old && ['uploading', 'starting', 'hashing'].includes(old.status)) { toast(`"${file.name}" is already uploading.`, 'warn'); return old; }
  const t = {
    key, file, filename: file.name, size: file.size, lastModified: file.lastModified, id: null, chunkSize: 0,
    status: 'starting', received: new Set(), total: 0, sentBytes: 0, startBytes: 0, samples: [], rate: 0, eta: null,
    retries: 0, error: null, pstId: null, onValid, ctl: { cancel: false, ac: new AbortController() },
  };
  tasks.set(key, t); changed();
  run(t);
  return t;
}

function bytesDone(t) {
  let b = 0;
  for (const n of t.received) b += Math.min(t.chunkSize, t.size - n * t.chunkSize);
  return b;
}

async function run(t) {
  const ctl = t.ctl = { cancel: false, ac: new AbortController() };
  try {
    t.error = null;
    t.status = 'starting'; changed();
    const info = await retrying(t, ctl, () => post('/uploads', { filename: t.filename, size: t.size, last_modified: t.lastModified }, { signal: ctl.ac.signal }));
    if (ctl.cancel) return;
    t.id = info.id; t.chunkSize = info.chunk_size;
    t.total = Math.max(1, Math.ceil(t.size / t.chunkSize));
    t.received = new Set(info.received || []);
    t.resumedFrom = t.received.size;
    lsSet(t);
    t.sentBytes = bytesDone(t); t.startBytes = t.sentBytes; t.samples = [[performance.now(), t.sentBytes]];
    t.status = 'uploading'; changed();

    const todo = []; for (let n = 0; n < t.total; n++) if (!t.received.has(n)) todo.push(n);
    let failed = null;
    const worker = async () => {
      while (!ctl.cancel && !failed && todo.length) {
        const n = todo.shift();
        try { await sendChunk(t, ctl, n); } catch (e) { failed = e; todo.unshift(n); }
      }
    };
    await Promise.all(Array.from({ length: PARALLEL }, worker));
    if (ctl.cancel) { if (t.ctl === ctl) { t.status = ctl.reason || 'paused'; changed(); } return; }
    if (failed) throw failed;

    // final server-side check (in case chunks are still missing)
    const st = await retrying(t, ctl, () => get(`/uploads/${t.id}`, { signal: ctl.ac.signal }));
    if (ctl.cancel) return;
    const missing = []; for (let n = 0; n < t.total; n++) if (!(st.received || []).includes(n)) missing.push(n);
    if (missing.length) { t.received = new Set(st.received); return run(t); }

    t.status = 'completing'; changed();
    const res = await retrying(t, ctl, () => post(`/uploads/${t.id}/complete`, undefined, { signal: ctl.ac.signal }));
    if (ctl.cancel) return;
    t.pstId = res?.pst_id ?? res?.pst?.id ?? res?.id ?? t.id;
    t.status = 'done'; t.rate = 0; t.eta = 0; lsDel(t.key); changed();
    toast(`"${t.filename}" uploaded, validating…`, 'ok');
    if (t.onValid) watchPst(t);
  } catch (e) {
    if (ctl.cancel) return;
    t.status = 'error'; t.error = e.message || String(e); changed();
    toast(`Upload of "${t.filename}" failed: ${t.error}. You can resume it.`, 'bad', 8000);
  }
}

async function watchPst(t) {
  const finish = async p => {
    if (p.status === 'valid') { try { await t.onValid(p); } catch (e) { toast(e.message, 'bad'); } }
    return p.status === 'valid' || p.status === 'invalid';
  };
  const off = on('pst', async p => { if (p.id === t.pstId && await finish(p)) off(); });
  for (let i = 0; i < 600; i++) { // safety net if SSE is unavailable
    await sleep(3000);
    try { const p = await get('/psts/' + t.pstId); if (p.status === 'valid' || p.status === 'invalid') { off(); return finish(p); } } catch {}
  }
  off();
}

async function sendChunk(t, ctl, n) {
  const start = n * t.chunkSize, end = Math.min(t.size, start + t.chunkSize);
  const buf = await t.file.slice(start, end).arrayBuffer();
  const hash = await sha256Hex(buf);
  for (let attempt = 0; ; attempt++) {
    if (ctl.cancel) throw new Error('cancelled');
    try {
      await put(`/uploads/${t.id}/chunks/${n}`, buf, { headers: { 'X-Chunk-Sha256': hash, 'Content-Type': 'application/octet-stream' }, noAuthRedirect: false, signal: ctl.ac.signal });
      if (ctl.cancel) throw new Error('cancelled');
      t.received.add(n); t.retries = 0;
      t.sentBytes += end - start; sample(t); lsSet(t); changed();
      return;
    } catch (e) {
      if (ctl.cancel) throw new Error('cancelled');
      if (e instanceof ApiError && [400, 401, 403, 404, 413].includes(e.status)) throw e; // not retryable
      t.retries++;
      if (attempt + 1 >= MAX_TRIES) throw e;
      t.status = 'uploading'; t.warn = `Retrying (${attempt + 1}/${MAX_TRIES - 1})…`; changed();
      await backoff(ctl, attempt);
      t.warn = null;
    }
  }
}

async function retrying(t, ctl, fn) {
  for (let a = 0; ; a++) {
    try { return await fn(); } catch (e) {
      if (ctl.cancel) throw new Error('cancelled');
      if ((e instanceof ApiError && e.status < 500 && e.status !== 408 && e.status !== 429) || a + 1 >= MAX_TRIES) throw e;
      await backoff(ctl, a);
    }
  }
}
async function backoff(ctl, a) {
  const ms = Math.min(20000, 800 * 2 ** a) * (0.7 + Math.random() * 0.6);
  const end = Date.now() + ms;
  while (Date.now() < end && !ctl.cancel) await sleep(200);
}

function sample(t) {
  const now = performance.now();
  t.samples.push([now, t.sentBytes]);
  while (t.samples.length > 2 && now - t.samples[0][0] > 8000) t.samples.shift();
  const [t0, b0] = t.samples[0];
  const dt = (now - t0) / 1000;
  if (dt > 0.5) { t.rate = (t.sentBytes - b0) / dt; t.eta = t.rate > 0 ? (t.size - t.sentBytes) / t.rate : null; }
}

export function pause(t) { t.ctl.cancel = true; t.ctl.ac.abort(); t.ctl.reason = 'paused'; t.status = 'paused'; t.rate = 0; changed(); }
export function resume(t) { if (['paused', 'error'].includes(t.status)) run(t); }
export function cancel(t) { t.ctl.cancel = true; t.ctl.ac.abort(); t.ctl.reason = 'cancelled'; tasks.delete(t.key); lsDel(t.key); changed(); }
export function dismiss(t) { tasks.delete(t.key); changed(); }

addEventListener('beforeunload', e => {
  if (hasActiveUploads()) { e.preventDefault(); e.returnValue = 'A PST upload is in progress. Leaving the page will interrupt it (it can be resumed).'; return e.returnValue; }
});
addEventListener('online', () => { for (const t of tasks.values()) if (t.status === 'error') resume(t); });
