// Mini UI library: h(), icons, toasts, modals, formatting.
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'indeterminate') el[k] = v;
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export const $ = (s, r = document) => r.querySelector(s);
export function clear(el) { el.replaceChildren(); return el; }

const ICONS = {
  dash: 'M3 13h8V3H3zm0 8h8v-6H3zm10 0h8V11h-8zm0-18v6h8V3z',
  server: 'M4 4h16v6H4zm0 10h16v6H4zM7 7h.01M7 17h.01',
  users: 'M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0zM4 21a8 8 0 0 1 16 0',
  upload: 'M12 16V4m0 0L7 9m5-5l5 5M4 20h16',
  plus: 'M12 5v14M5 12h14', trash: 'M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3',
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4', check: 'M5 12l5 5 9-10', x: 'M6 6l12 12M18 6L6 18',
  play: 'M7 4l13 8-13 8z', pause: 'M8 4v16M16 4v16', stop: 'M6 6h12v12H6z', retry: 'M4 12a8 8 0 1 1 3 6.2M4 20v-6h6',
  download: 'M12 4v12m0 0l-5-5m5 5l5-5M4 20h16', test: 'M9 3h6M10 3v6l-5 10a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-10V3',
  logo: 'M4.5 5h11A2.5 2.5 0 0 1 18 7.5v6a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 2 13.5v-6A2.5 2.5 0 0 1 4.5 5zM2.5 7l8 5.5 8-5.5M12 20h9M18 17l3 3-3 3',
  out: 'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10',
  cog: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3m0 14v3M4.9 4.9l2.1 2.1m10 10l2.1 2.1M2 12h3m14 0h3M4.9 19.1L7 17M17 7l2.1-2.1',
  file: 'M6 3h8l4 4v14H6zM14 3v4h4', folder: 'M3 6h6l2 2h10v11H3z', alert: 'M12 3l10 18H2zM12 10v5m0 3h.01',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z', menu: 'M4 7h16M4 12h16M4 17h16',
  chev: 'M9 6l6 6-6 6', link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1-1',
};
export function icon(name, size = 18) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size);
  s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '1.8');
  s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', ICONS[name] || ''); s.append(p);
  return s;
}
export const btn = (label, { ico, kind = '', onClick, title, disabled, type = 'button' } = {}) =>
  h('button', { class: 'btn ' + kind, type, onClick, title, disabled }, ico && icon(ico, 16), label && h('span', null, label));

// ---------- formats
export function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)).toString() + ' ' + u[i];
}
export function fmtDur(s) {
  if (s == null || !isFinite(s)) return '—';
  s = Math.round(s); if (s < 60) return s + 's';
  const m = Math.floor(s / 60); if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
  const hh = Math.floor(m / 60); return hh + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
export function fmtDate(d) {
  if (!d) return '—';
  const x = typeof d === 'number' ? new Date(d < 1e12 ? d * 1000 : d) : new Date(d);
  return isNaN(x) ? String(d) : x.toLocaleString('en-US', { dateStyle: 'short', timeStyle: 'medium' });
}
export const fmtNum = n => n == null ? '—' : Number(n).toLocaleString('en-US');
export const pct = (done, total) => total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;

// ---------- badges
const JOB = { queued: ['Queued', 'muted'], running: ['Running', 'info'], paused: ['Paused', 'warn'], stopping: ['Stopping…', 'warn'],
  stopped: ['Stopped', 'muted'], done: ['Done', 'ok'], done_with_errors: ['Done with errors', 'warn'], failed: ['Failed', 'bad'] };
const PST = { uploading: ['Uploading', 'info'], validating: ['Validating…', 'info'], valid: ['Valid PST', 'ok'], invalid: ['Invalid', 'bad'] };
const ACC = { new: ['Not tested', 'muted'], ok: ['OK', 'ok'], auth_failed: ['Auth refused', 'bad'], error: ['Error', 'bad'] };
export const PHASES = { precheck: 'Pre-checks', mail: 'Mail', calendar: 'Calendar', contacts: 'Contacts', verify: 'Verification' };
const badge = (map, k) => { const [t, c] = map[k] || [k ?? '—', 'muted']; return h('span', { class: 'badge ' + c }, h('i'), t); };
export const jobBadge = k => badge(JOB, k);
export const pstBadge = k => badge(PST, k);
export const accBadge = k => badge(ACC, k);
export const isActive = s => ['queued', 'running', 'paused', 'stopping'].includes(s);

export function progressBar(value, { label, kind = '', striped = false } = {}) {
  return h('div', { class: 'pbar ' + kind + (striped ? ' striped' : '') }, h('div', { style: { width: Math.max(0, Math.min(100, value)) + '%' } }), label && h('span', null, label));
}

// ---------- toasts
export function toast(msg, kind = 'info', ms = 4500) {
  const t = h('div', { class: 'toast ' + kind, role: 'status' }, h('span', null, msg), h('button', { class: 'x', onClick: () => t.remove(), 'aria-label': 'Close' }, '×'));
  document.getElementById('toasts').append(t);
  if (ms) setTimeout(() => t.remove(), ms);
}

// ---------- modals
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const root = document.getElementById('modals');
  const close = () => { back.remove(); document.removeEventListener('keydown', esc); onClose?.(); };
  const esc = e => { if (e.key === 'Escape') close(); };
  const box = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true' },
    h('header', null, h('h3', null, title), h('button', { class: 'x', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { class: 'mbody' }, body),
    actions.length ? h('footer', null, actions.map(a => btn(a.label, { kind: a.kind, ico: a.ico, onClick: () => a.onClick(close) }))) : null);
  const back = h('div', { class: 'backdrop', onMousedown: e => { if (e.target === back) close(); } }, box);
  root.append(back); document.addEventListener('keydown', esc);
  setTimeout(() => box.querySelector('input,select,textarea,button.primary')?.focus(), 30);
  return { close, box };
}
export function confirmBox(title, text, { okLabel = 'Confirm', danger = false } = {}) {
  return new Promise(res => {
    let done = false;
    modal({ title, body: h('p', null, text), onClose: () => { if (!done) res(false); },
      actions: [{ label: 'Cancel', onClick: c => c() }, { label: okLabel, kind: danger ? 'danger' : 'primary', onClick: c => { done = true; res(true); c(); } }] });
  });
}
export function field(label, input, hint) {
  return h('label', { class: 'field' }, h('span', { class: 'lbl' }, label), input, hint && h('small', null, hint));
}
export function empty(text, sub) { return h('div', { class: 'empty' }, h('div', { class: 'big' }, text), sub && h('p', null, sub)); }
export function debounce(fn, ms = 200) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function download(name, content, type = 'application/json') {
  const a = h('a', { href: URL.createObjectURL(new Blob([content], { type })), download: name });
  document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}
