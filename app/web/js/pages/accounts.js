import { get, post, put, del, on, api } from '../api.js';
import { h, clear, btn, icon, modal, field, toast, confirmBox, empty, accBadge, pstBadge, progressBar, pct, fmtDate, fmtBytes, download, isActive, jobBadge } from '../ui.js';
import { totals } from '../jobview.js';
import { addFile, tasks } from '../uploader.js';

const results = new Map(); // account id -> last test result (dots)

export default async function (root, { query }) {
  const servers = await get('/servers');
  if (!servers.length) { root.append(h('div', { class: 'page-h' }, h('h1', null, 'Accounts')), empty('No server configured', 'Create a server first.'), btn('Go to servers', { onClick: () => location.hash = '#/servers' })); return; }
  let sid = Number(query.get('server')) || Number(sessionStorage.getItem('server')) || servers[0].id;
  if (!servers.some(s => s.id === sid)) sid = servers[0].id;
  let accounts = [], psts = [];
  const sel = new Set();
  const search = h('input', { type: 'search', placeholder: 'Filter…', onInput: () => draw() });
  const serverSel = h('select', { onChange: e => { sid = Number(e.target.value); sessionStorage.setItem('server', sid); sel.clear(); load(); } },
    servers.map(s => h('option', { value: s.id, selected: s.id === sid }, s.name)));
  const bar = h('div', { class: 'toolbar' });
  const tbody = h('tbody');
  const allBox = h('input', { type: 'checkbox', 'aria-label': 'Select all', onChange: e => { visible().forEach(a => e.target.checked ? sel.add(a.id) : sel.delete(a.id)); draw(); } });
  root.append(h('div', { class: 'page-h' }, h('h1', null, 'Accounts'), h('p', { class: 'muted' }, 'Mailboxes to migrate, their PST file and import options.')),
    h('div', { class: 'toolbar' }, h('label', { class: 'inline' }, 'Server', serverSel), search, h('span', { class: 'sp' }),
      btn('Add', { ico: 'plus', onClick: () => addModal() }), btn('Import CSV', { ico: 'upload', onClick: csvModal }),
      btn('Test all', { ico: 'test', onClick: testAll })),
    bar,
    h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', { class: 'chk' }, allBox), ['Account', 'Connection', 'PST file', 'Content', 'Progress', ''].map(t => h('th', null, t)))), tbody)));

  const visible = () => { const q = search.value.trim().toLowerCase(); return accounts.filter(a => !q || a.email.toLowerCase().includes(q)); };
  async function load() {
    [accounts, psts] = await Promise.all([get('/accounts?server_id=' + sid), get('/psts')]);
    draw();
  }
  const pstById = id => psts.find(p => p.id === id);
  const dots = a => {
    const r = results.get(a.id);
    const d = (n, x) => h('span', { class: 'dot ' + (x ? (x.ok ? 'ok' : 'bad') : 'off'), title: `${n} : ${x ? (x.ok ? 'OK' : 'Failed') + (x.msg ? ' — ' + x.msg : '') : 'not tested'}${x?.quota ? ' · quota ' + JSON.stringify(x.quota) : ''}` }, n);
    return h('div', { class: 'dots' }, d('IMAP', r?.imap), d('CalDAV', r?.caldav), d('CardDAV', r?.carddav));
  };

  function draw() {
    const rows = visible();
    clear(tbody);
    if (!rows.length) tbody.append(h('tr', null, h('td', { colspan: 7 }, empty('No accounts', 'Add accounts manually or via CSV import.'))));
    for (const a of rows) {
      const p = pstById(a.pst_id);
      const task = [...tasks.values()].find(t => t.accountId === a.id && t.status !== 'done' || (t.accountId === a.id && t.pstId && !p));
      const validPsts = psts.filter(x => x.status === 'valid' || x.id === a.pst_id);
      const pstSel = h('select', { class: 'pst-sel', onChange: async e => {
        if (e.target.value === '__up') { e.target.value = a.pst_id ?? ''; return pick(a); }
        try { await put(`/accounts/${a.id}/pst`, { pst_id: e.target.value ? Number(e.target.value) || e.target.value : null }); await load(); } catch (x) { toast(x.message, 'bad'); }
      } }, h('option', { value: '' }, '— none —'),
        validPsts.map(x => h('option', { value: x.id, selected: x.id === a.pst_id }, `${x.filename} (${fmtBytes(x.size)})`)),
        h('option', { value: '__up' }, 'Upload a PST…'));
      const opt = k => h('label', { class: 'mini-check', title: { mail: 'Mail', calendar: 'Calendar', contacts: 'Contacts' }[k] },
        h('input', { type: 'checkbox', checked: !!a.options?.[k], onChange: async e => {
          const options = { ...a.options, [k]: e.target.checked };
          try { await put('/accounts/' + a.id, { options }); a.options = options; } catch (x) { e.target.checked = !e.target.checked; toast(x.message, 'bad'); } } }),
        { mail: 'Mail', calendar: 'Calendar', contacts: 'Contacts' }[k]);
      const j = a.job;
      const t = j && totals(j);
      tbody.append(h('tr', { class: sel.has(a.id) ? 'sel' : '' },
        h('td', { class: 'chk' }, h('input', { type: 'checkbox', checked: sel.has(a.id), onChange: e => { e.target.checked ? sel.add(a.id) : sel.delete(a.id); drawBar(); e.target.closest('tr').classList.toggle('sel', e.target.checked); } })),
        h('td', null, h('b', null, a.email), a.login && a.login !== a.email ? h('div', { class: 'muted small' }, 'login: ' + a.login) : null, h('div', null, accBadge(a.status), a.last_test ? h('span', { class: 'muted small' }, ' ' + fmtDate(a.last_test)) : null)),
        h('td', null, dots(a)),
        h('td', null, task && task.status !== 'done' ? h('div', { class: 'up-inline' }, progressBar(pct(task.sentBytes, task.size), { label: `${task.filename} — ${pct(task.sentBytes, task.size)}%`, striped: true }),
              task.status === 'error' ? h('small', { class: 'err' }, task.error) : null) : [pstSel, p ? pstBadge(p.status) : null]),
        h('td', null, h('div', { class: 'opts' }, ['mail', 'calendar', 'contacts'].map(opt))),
        h('td', null, j ? h('a', { href: '#/jobs/' + j.id, class: 'prog-link' }, jobBadge(j.status), progressBar(t.pct, { striped: j.status === 'running', kind: t.errors ? 'warn' : '' })) : h('span', { class: 'muted' }, '—')),
        h('td', { class: 'acts' }, btn('', { ico: 'test', title: 'Test connection', onClick: () => testOne(a) }), btn('', { ico: 'edit', title: 'Edit password', onClick: () => addModal(a) }),
          btn('', { ico: 'trash', kind: 'danger-ghost', title: 'Delete', onClick: () => remove(a) }))));
    }
    allBox.checked = rows.length > 0 && rows.every(a => sel.has(a.id));
    drawBar();
  }
  function drawBar() {
    clear(bar);
    if (!sel.size) return;
    bar.append(h('div', { class: 'selbar' }, h('b', null, `${sel.size} selected`), h('span', { class: 'sp' }),
      btn('Test selection', { ico: 'test', onClick: () => testMany(accounts.filter(a => sel.has(a.id))) }),
      btn('Start import', { ico: 'play', kind: 'primary', onClick: launch })));
  }

  async function launch() {
    const chosen = accounts.filter(a => sel.has(a.id));
    const noPst = chosen.filter(a => !a.pst_id), busy = chosen.filter(a => a.job && isActive(a.job.status));
    if (noPst.length) return toast(`${noPst.length} account(s) without a PST: ${noPst.slice(0, 3).map(a => a.email).join(', ')}${noPst.length > 3 ? '…' : ''}`, 'warn', 7000);
    if (busy.length) return toast(`${busy.length} account(s) already have an active job.`, 'warn');
    if (!await confirmBox('Start import', `Import ${chosen.length} mailbox(es)? Items already imported are skipped (no duplicates).`, { okLabel: 'Start' })) return;
    try { const r = await post('/runs', { account_ids: chosen.map(a => a.id) }); toast('Import queued.', 'ok'); sel.clear(); await load(); if (Array.isArray(r) && r.length === 1 && r[0].id) location.hash = '#/jobs/' + r[0].id; }
    catch (e) { toast(e.message, 'bad'); }
  }
  async function testOne(a, quiet) {
    try { const r = await post(`/accounts/${a.id}/test`); results.set(a.id, r); if (!quiet) { toast(`${a.email}: ${r.imap?.ok ? 'IMAP OK' : 'IMAP failed — ' + (r.imap?.msg || '')}`, r.imap?.ok ? 'ok' : 'bad'); } return r; }
    catch (e) { if (!quiet) toast(e.message, 'bad'); }
    finally { if (!quiet) load(); }
  }
  async function testMany(list) {
    if (!list.length) return;
    toast(`Testing ${list.length} account(s)…`, 'info', 2000);
    let i = 0, ok = 0;
    await Promise.all(Array.from({ length: Math.min(4, list.length) }, async () => { while (i < list.length) { const r = await testOne(list[i++], true); if (r?.imap?.ok) ok++; draw(); } }));
    toast(`${ok}/${list.length} account(s) with IMAP OK.`, ok === list.length ? 'ok' : 'warn');
    load();
  }
  function testAll() { return testMany(accounts); }
  async function remove(a) {
    if (!await confirmBox('Delete account', `Delete ${a.email}?`, { okLabel: 'Delete', danger: true })) return;
    try { await del('/accounts/' + a.id); sel.delete(a.id); toast('Account deleted.', 'ok'); load(); } catch (e) { toast(e.message, 'bad'); }
  }
  function pick(a) {
    const inp = h('input', { type: 'file', accept: '.pst,.ost', onChange: () => {
      const f = inp.files[0]; if (!f) return;
      const t = addFile(f, { onValid: async p => { await put(`/accounts/${a.id}/pst`, { pst_id: p.id }); toast(`PST assigned to ${a.email}.`, 'ok'); load(); } });
      t.accountId = a.id; draw();
    } });
    inp.click();
  }
  function addModal(a) {
    const s = { email: a?.email || '', password: '', login: a?.login || '' };
    const form = h('form', { class: 'form', onSubmit: e => e.preventDefault() },
      field('Email address', h('input', { type: 'email', required: true, value: s.email, disabled: !!a, onInput: e => s.email = e.target.value })),
      field('Password', h('input', { type: 'password', required: !a, autocomplete: 'new-password', placeholder: a ? 'Leave empty to keep the current one' : '', onInput: e => s.password = e.target.value })),
      field('Login (optional)', h('input', { value: s.login, placeholder: 'default: the email address', onInput: e => s.login = e.target.value })));
    modal({ title: a ? 'Edit ' + a.email : 'Add an account', body: form, actions: [{ label: 'Cancel', onClick: c => c() },
      { label: a ? 'Save' : 'Add and test', kind: 'primary', onClick: async c => {
        if (!form.reportValidity()) return;
        try {
          const body = { ...(a ? {} : { server_id: sid, email: s.email }), ...(s.password ? { password: s.password } : {}), ...(s.login ? { login: s.login } : {}) };
          const acc = a ? await put('/accounts/' + a.id, body) : await post('/accounts', body);
          c(); toast('Account saved.', 'ok'); await load(); const id = acc?.id ?? a?.id; const x = accounts.find(z => z.id === id); if (x) testOne(x);
        } catch (e) { toast(e.message, 'bad'); } } }] });
  }
  function csvModal() {
    const out = h('div');
    const file = h('input', { type: 'file', accept: '.csv,.txt' });
    modal({ title: 'Import CSV', wide: true, body: h('div', null,
      h('p', null, 'Columns: ', h('code', null, 'email;password[;login]'), ' — separator ";" or ",", header optional. Target server: ', h('b', null, servers.find(s => s.id === sid)?.name)),
      h('p', null, h('a', { href: '#', onClick: async e => { e.preventDefault(); try { download('accounts.csv', await get('/accounts/csv-template'), 'text/csv'); } catch (x) { toast(x.message, 'bad'); } } }, 'Download CSV template')),
      file, out),
      actions: [{ label: 'Close', onClick: c => c() }, { label: 'Import', kind: 'primary', ico: 'upload', onClick: async () => {
        if (!file.files[0]) return toast('Choose a CSV file.', 'warn');
        const fd = new FormData(); fd.append('file', file.files[0]); fd.append('server_id', sid);
        try {
          const r = await post('/accounts/import-csv', fd);
          clear(out).append(h('div', { class: 'csv-res' }, h('span', { class: 'badge ok' }, h('i'), `${r.created} created`), h('span', { class: 'badge info' }, h('i'), `${r.updated} updated`), h('span', { class: 'badge ' + (r.errors?.length ? 'bad' : 'muted') }, h('i'), `${r.errors?.length || 0} error(s)`)),
            r.errors?.length ? h('table', { class: 'tbl small' }, h('thead', null, h('tr', null, h('th', null, 'Line'), h('th', null, 'Error'))), h('tbody', null, r.errors.map(e => h('tr', null, h('td', null, e.line), h('td', null, e.msg))))) : null);
          load();
        } catch (e) { toast(e.message, 'bad'); } } }] });
  }

  await load();
  let tm; const soon = () => { clearTimeout(tm); tm = setTimeout(draw, 250); };
  const offs = [
    on('account', a => { if (a.server_id !== sid) return; const i = accounts.findIndex(x => x.id === a.id); i < 0 ? accounts.push(a) : accounts[i] = a; soon(); }),
    on('job', j => { const a = accounts.find(x => x.id === j.account_id); if (a) { a.job = j; soon(); } }),
    on('pst', p => { const i = psts.findIndex(x => x.id === p.id); i < 0 ? psts.push(p) : psts[i] = p; soon(); }),
    on('uploader', () => { if ([...tasks.values()].some(t => t.accountId != null)) { if (!tm || true) { clearTimeout(tm); tm = setTimeout(draw, 700); } } }),
  ];
  return () => { clearTimeout(tm); offs.forEach(f => f()); };
}
