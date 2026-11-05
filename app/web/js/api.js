export class ApiError extends Error {
  constructor(status, msg, body) { super(msg); this.status = status; this.body = body; }
}
export const bus = new EventTarget();
export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));
export const on = (type, fn) => { const f = e => fn(e.detail); bus.addEventListener(type, f); return () => bus.removeEventListener(type, f); };

export async function api(method, path, body, opts = {}) {
  const init = { method, credentials: 'same-origin', headers: {}, signal: opts.signal };
  if (body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
  Object.assign(init.headers, opts.headers || {});
  const r = await fetch('/api' + path, init);
  if (r.status === 401 && !opts.noAuthRedirect) emit('unauthorized');
  if (!r.ok) {
    let b = null, msg = r.statusText;
    try { b = await r.json(); const d = b.detail ?? b.message ?? b.error; msg = typeof d === 'string' ? d : (d ? JSON.stringify(d) : msg); } catch {}
    throw new ApiError(r.status, msg || ('HTTP ' + r.status), b);
  }
  if (opts.raw) return r;
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}
export const get = (p, o) => api('GET', p, undefined, o);
export const post = (p, b, o) => api('POST', p, b ?? {}, o);
export const put = (p, b, o) => api('PUT', p, b, o);
export const del = (p, o) => api('DELETE', p, undefined, o);

// SSE with automatic reconnection (backoff); emits on the bus: job / upload / pst / account / sse-state
let es = null, retry = 0, timer = null, wanted = false;
export function startEvents() {
  wanted = true; if (es) return;
  es = new EventSource('/api/events');
  es.onopen = () => { retry = 0; emit('sse-state', true); emit('sse-open'); };
  for (const t of ['job', 'upload', 'pst', 'account']) {
    es.addEventListener(t, e => { try { emit(t, JSON.parse(e.data)); } catch {} });
  }
  es.onerror = () => {
    emit('sse-state', false);
    es.close(); es = null;
    if (!wanted) return;
    clearTimeout(timer);
    timer = setTimeout(startEvents, Math.min(15000, 1000 * 2 ** retry++));
  };
}
export function stopEvents() { wanted = false; clearTimeout(timer); es?.close(); es = null; }
