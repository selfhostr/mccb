import { get, post, on, startEvents, stopEvents } from './api.js';
import { h, icon, btn, toast, clear, modal } from './ui.js';
import './uploader.js';
import { hasActiveUploads } from './uploader.js';

const app = document.getElementById('app');
let appVersion = '';
get('/version', { noAuthRedirect: true }).then(r => { appVersion = r.version ? 'v' + r.version : ''; document.querySelectorAll('.ver').forEach(e => { e.textContent = appVersion; }); }).catch(() => {});
let cleanup = null, me = null, sseOk = false, routeSeq = 0;

const routes = [
  [/^#\/?$|^#\/dashboard/, 'dashboard', 'Dashboard', 'dash', () => import('./pages/dashboard.js')],
  [/^#\/servers/, 'servers', 'Servers', 'server', () => import('./pages/servers.js')],
  [/^#\/accounts/, 'accounts', 'Accounts', 'users', () => import('./pages/accounts.js')],
  [/^#\/uploads/, 'uploads', 'PST files', 'upload', () => import('./pages/uploads.js')],
  [/^#\/settings/, 'settings', 'Settings', 'cog', () => import('./pages/settings.js')],
  [/^#\/jobs\/(\d+)/, 'jobs', 'Job details', 'dash', () => import('./pages/job.js')],
];
const nav = [['dashboard', 'Dashboard', 'dash', '#/dashboard'], ['servers', 'Servers', 'server', '#/servers'],
  ['accounts', 'Accounts', 'users', '#/accounts'], ['uploads', 'PST files', 'upload', '#/uploads'],
  ['settings', 'Settings', 'cog', '#/settings']];

function shell() {
  clear(app);
  const content = h('main', { id: 'content' });
  const dot = h('span', { class: 'live', id: 'live', title: 'Live updates' });
  const side = h('aside', { class: 'side' },
    h('div', { class: 'brand' }, h('div', { class: 'logo' }, icon('logo', 18)), h('div', null, h('b', null, 'MCCB'), h('small', null, 'Mail Contact Calendrier Bridge'))),
    h('nav', null, nav.map(([k, t, i, href]) => h('a', { href, 'data-nav': k }, icon(i), h('span', null, t)))),
    h('div', { class: 'side-foot' }, dot, h('span', { id: 'livetxt' }, 'Connecting…'),
      h('span', { class: 'sp' }), h('span', { class: 'ver' }, appVersion),
      h('button', { class: 'icon-btn', title: 'Sign out', onClick: async () => { try { await post('/logout'); } catch {} stopEvents(); showLogin(); } }, icon('out', 16))));
  const top = h('header', { class: 'topbar' }, h('button', { class: 'icon-btn', onClick: () => document.body.classList.toggle('navopen') }, icon('menu')), h('b', null, 'MCCB'));
  app.append(h('div', { class: 'layout' }, top, side, content));
  side.addEventListener('click', e => { if (e.target.closest('a')) document.body.classList.remove('navopen'); });
  setLive(sseOk);
}
function setLive(ok) {
  sseOk = ok; const d = document.getElementById('live'), t = document.getElementById('livetxt');
  if (d) { d.classList.toggle('on', ok); t.textContent = ok ? 'Live' : 'Reconnecting…'; }
}
on('sse-state', setLive);

async function route() {
  if (!me) return;
  const seq = ++routeSeq;
  cleanup?.(); cleanup = null;
  const hash = location.hash || '#/dashboard';
  const r = routes.find(r => r[0].test(hash)) || routes[0];
  const m = hash.match(r[0]);
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === (r[1] === 'jobs' ? 'dashboard' : r[1])));
  const c = document.getElementById('content'); if (!c) return;
  clear(c); c.append(h('div', { class: 'loading' }, 'Loading…'));
  try {
    const mod = await r[4]();
    if (location.hash !== hash && (location.hash || '#/dashboard') !== hash) return;
    if (seq !== routeSeq) return;
    const box = h('div');
    const cl = await mod.default(box, { params: m.slice(1), query: new URLSearchParams(hash.split('?')[1] || '') }) || null;
    if (seq !== routeSeq) { cl?.(); return; }
    clear(c).append(box); cleanup = cl;
  } catch (e) {
    console.error(e); if (seq !== routeSeq) return; clear(c); c.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, 'Failed to load'), h('p', null, e.message)));
  }
}

function showLogin(msg) {
  me = null; cleanup?.(); cleanup = null; clear(app); stopEvents();
  const pw = h('input', { type: 'password', placeholder: 'Admin password', autocomplete: 'current-password', required: true, autofocus: true });
  const err = h('div', { class: 'form-err' }, msg || '');
  const form = h('form', { class: 'login-card', onSubmit: async e => {
    e.preventDefault(); err.textContent = '';
    try { await post('/login', { password: pw.value }, { noAuthRedirect: true }); await boot(); }
    catch (x) { err.textContent = x.status === 401 || x.status === 403 ? 'Incorrect password.' : x.message; pw.select(); }
  } },
    h('div', { class: 'logo big' }, icon('logo', 28)), h('h1', null, 'MCCB'),
    h('p', { class: 'muted' }, 'Mail Contact Calendrier Bridge'), pw, err,
    btn('Sign in', { kind: 'primary', type: 'submit' }), h('small', { class: 'ver' }, appVersion));
  app.append(h('div', { class: 'login' }, form));
}

async function boot() {
  try { me = await get('/me', { noAuthRedirect: true }); } catch { return showLogin(); }
  shell(); startEvents(); route();
}
on('unauthorized', () => { if (me) showLogin('Session expired. Please sign in again.'); });
addEventListener('hashchange', route);
boot();
