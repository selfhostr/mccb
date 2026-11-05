import { get, post, on, startEvents, stopEvents } from './api.js';
import { h, icon, btn, toast, clear, modal } from './ui.js';
import './uploader.js';
import { hasActiveUploads } from './uploader.js';

const app = document.getElementById('app');
let cleanup = null, me = null, sseOk = false;

const routes = [
  [/^#\/?$|^#\/dashboard/, 'dashboard', 'Tableau de bord', 'dash', () => import('./pages/dashboard.js')],
  [/^#\/servers/, 'servers', 'Serveurs', 'server', () => import('./pages/servers.js')],
  [/^#\/accounts/, 'accounts', 'Comptes', 'users', () => import('./pages/accounts.js')],
  [/^#\/uploads/, 'uploads', 'Fichiers PST', 'upload', () => import('./pages/uploads.js')],
  [/^#\/jobs\/(\d+)/, 'jobs', 'Détail du job', 'dash', () => import('./pages/job.js')],
];
const nav = [['dashboard', 'Tableau de bord', 'dash', '#/dashboard'], ['servers', 'Serveurs', 'server', '#/servers'],
  ['accounts', 'Comptes', 'users', '#/accounts'], ['uploads', 'Fichiers PST', 'upload', '#/uploads']];

function theme() {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  const nx = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = nx; localStorage.setItem('theme', nx);
}

function shell() {
  clear(app);
  const content = h('main', { id: 'content' });
  const dot = h('span', { class: 'live', id: 'live', title: 'Temps réel' });
  const side = h('aside', { class: 'side' },
    h('div', { class: 'brand' }, h('div', { class: 'logo' }, icon('check', 18)), h('div', null, h('b', null, 'PST Migrator'), h('small', null, 'PST → IMAP · CalDAV · CardDAV'))),
    h('nav', null, nav.map(([k, t, i, href]) => h('a', { href, 'data-nav': k }, icon(i), h('span', null, t)))),
    h('div', { class: 'side-foot' }, dot, h('span', { id: 'livetxt' }, 'Connexion…'),
      h('span', { class: 'sp' }),
      h('button', { class: 'icon-btn', title: 'Thème clair/sombre', onClick: theme }, icon('sun', 16)),
      h('button', { class: 'icon-btn', title: 'Déconnexion', onClick: async () => { try { await post('/logout'); } catch {} stopEvents(); showLogin(); } }, icon('out', 16))));
  const top = h('header', { class: 'topbar' }, h('button', { class: 'icon-btn', onClick: () => document.body.classList.toggle('navopen') }, icon('menu')), h('b', null, 'PST Migrator'));
  app.append(h('div', { class: 'layout' }, top, side, content));
  side.addEventListener('click', e => { if (e.target.closest('a')) document.body.classList.remove('navopen'); });
  setLive(sseOk);
}
function setLive(ok) {
  sseOk = ok; const d = document.getElementById('live'), t = document.getElementById('livetxt');
  if (d) { d.classList.toggle('on', ok); t.textContent = ok ? 'Temps réel actif' : 'Reconnexion…'; }
}
on('sse-state', setLive);

async function route() {
  if (!me) return;
  cleanup?.(); cleanup = null;
  const hash = location.hash || '#/dashboard';
  const r = routes.find(r => r[0].test(hash)) || routes[0];
  const m = hash.match(r[0]);
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === (r[1] === 'jobs' ? 'dashboard' : r[1])));
  const c = document.getElementById('content'); if (!c) return;
  clear(c); c.append(h('div', { class: 'loading' }, 'Chargement…'));
  try {
    const mod = await r[4]();
    if (location.hash !== hash && (location.hash || '#/dashboard') !== hash) return;
    clear(c);
    cleanup = await mod.default(c, { params: m.slice(1), query: new URLSearchParams(hash.split('?')[1] || '') }) || null;
  } catch (e) {
    console.error(e); clear(c); c.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, 'Erreur de chargement'), h('p', null, e.message)));
  }
}

function showLogin(msg) {
  me = null; cleanup?.(); cleanup = null; clear(app); stopEvents();
  const pw = h('input', { type: 'password', placeholder: 'Mot de passe administrateur', autocomplete: 'current-password', required: true, autofocus: true });
  const err = h('div', { class: 'form-err' }, msg || '');
  const form = h('form', { class: 'login-card', onSubmit: async e => {
    e.preventDefault(); err.textContent = '';
    try { await post('/login', { password: pw.value }, { noAuthRedirect: true }); await boot(); }
    catch (x) { err.textContent = x.status === 401 || x.status === 403 ? 'Mot de passe incorrect.' : x.message; pw.select(); }
  } },
    h('div', { class: 'logo big' }, icon('check', 26)), h('h1', null, 'PST Migrator'),
    h('p', { class: 'muted' }, 'Migration de boîtes PST vers IMAP, CalDAV et CardDAV'), pw, err,
    btn('Se connecter', { kind: 'primary', type: 'submit' }));
  app.append(h('div', { class: 'login' }, form));
}

async function boot() {
  try { me = await get('/me', { noAuthRedirect: true }); } catch { return showLogin(); }
  shell(); startEvents(); route();
}
on('unauthorized', () => { if (me) showLogin('Session expirée, veuillez vous reconnecter.'); });
addEventListener('hashchange', route);
boot();
