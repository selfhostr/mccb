import { get, post, put, del } from '../api.js';
import { h, clear, btn, icon, modal, field, toast, confirmBox, empty } from '../ui.js';

const SEC = { ssl: 'Implicit SSL/TLS', starttls: 'STARTTLS', none: 'None (plaintext)' };
const blank = () => ({ name: '', imap_host: '', imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: '', caldav_url: '', carddav_url: '', dav_enabled: false, workers: 4, tuning: 'auto', builders: null, notes: '' });
const tuningText = s => s.tuning === 'manual' ? `Manual · ${s.workers} IMAP connection${s.workers > 1 ? 's' : ''} · ${s.builders ?? 'default'} builder${s.builders === 1 ? '' : 's'}` : 'Auto';

export default async function (root) {
  let servers = await get('/servers');
  const list = h('div', { class: 'grid cards' });
  root.append(h('div', { class: 'page-h row between' }, h('div', null, h('h1', null, 'Servers'), h('p', { class: 'muted' }, 'IMAP / CalDAV / CardDAV migration destinations.')),
    btn('New server', { ico: 'plus', kind: 'primary', onClick: () => edit(blank()) })), list);

  async function reload() { servers = await get('/servers'); draw(); }
  function draw() {
    clear(list);
    if (!servers.length) return list.append(empty('No servers', 'Add a server first to be able to create accounts.'));
    for (const s of servers) list.append(h('div', { class: 'card server' },
      h('div', { class: 'row between' }, h('b', null, s.name), h('span', { class: 'badge ' + (s.dav_enabled ? 'ok' : 'muted') }, h('i'), s.dav_enabled ? 'DAV enabled' : 'IMAP only')),
      h('dl', { class: 'kv' }, h('dt', null, 'IMAP'), h('dd', null, `${s.imap_host}:${s.imap_port} · ${SEC[s.imap_security] || s.imap_security}${s.verify_tls ? '' : ' · TLS not verified'}`),
        s.root_folder ? [h('dt', null, 'Root folder'), h('dd', null, s.root_folder)] : null,
        s.dav_enabled ? [h('dt', null, 'CalDAV'), h('dd', { class: 'mono' }, s.caldav_url || 'auto-discovery'), h('dt', null, 'CardDAV'), h('dd', { class: 'mono' }, s.carddav_url || 'auto-discovery')] : null,
        h('dt', null, 'Tuning'), h('dd', null, tuningText(s))),
      s.notes ? h('p', { class: 'muted small' }, s.notes) : null,
      h('div', { class: 'row gap actions' }, btn('Test', { ico: 'test', onClick: () => test(s) }), btn('Accounts', { ico: 'users', onClick: () => location.hash = '#/accounts?server=' + s.id }),
        h('span', { class: 'sp' }), btn('', { ico: 'edit', title: 'Edit', onClick: () => edit(s) }),
        btn('', { ico: 'trash', kind: 'danger-ghost', title: 'Delete', onClick: () => remove(s) }))));
  }
  async function test(s) {
    toast(`Testing "${s.name}"…`, 'info', 1500);
    try {
      const r = await post(`/servers/${s.id}/test`);
      const row = (n, x) => x ? h('li', { class: x.ok ? 'ok' : 'bad' }, h('b', null, n), ' ', x.ok ? '✔' : '✘', ' ', x.msg || '') : null;
      modal({ title: `Test of ${s.name}`, body: h('ul', { class: 'testlist' }, row('IMAP', r.imap), row('CalDAV', r.caldav), row('CardDAV', r.carddav),
        (!r.imap && r.ok != null) ? h('li', { class: r.ok ? 'ok' : 'bad' }, r.msg || (r.ok ? 'OK' : 'Failed')) : null), actions: [{ label: 'Close', onClick: c => c() }] });
    } catch (e) { toast(e.message, 'bad'); }
  }
  async function remove(s) {
    if (!await confirmBox('Delete server', `Delete "${s.name}"? Its accounts will also be deleted.`, { okLabel: 'Delete', danger: true })) return;
    try { await del('/servers/' + s.id); toast('Server deleted.', 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
  }
  function edit(s0) {
    const s = { ...s0 }, isNew = !s.id;
    const I = (k, o = {}) => h('input', { value: s[k] ?? '', onInput: e => { s[k] = o.type === 'number' ? Number(e.target.value) : e.target.value; }, ...o });
    const sec = h('select', { onChange: e => { s.imap_security = e.target.value; if (isNew || [993, 143].includes(+port.value)) { port.value = s.imap_port = e.target.value === 'ssl' ? 993 : 143; } } },
      Object.entries(SEC).map(([v, t]) => h('option', { value: v, selected: s.imap_security === v }, t)));
    const port = I('imap_port', { type: 'number', min: 1, max: 65535 });
    const dav = h('input', { type: 'checkbox', checked: !!s.dav_enabled, onChange: e => { s.dav_enabled = e.target.checked; davBox.hidden = !s.dav_enabled; } });
    const davBox = h('fieldset', { hidden: !s.dav_enabled },
      h('legend', null, 'CalDAV / CardDAV'),
      field('CalDAV URL', I('caldav_url', { placeholder: 'empty = auto-discovery (.well-known)' })),
      field('CardDAV URL', I('carddav_url', { placeholder: 'empty = auto-discovery (.well-known)' })),
      h('div', { class: 'help' }, h('b', null, 'Available placeholders'), h('ul', null,
        h('li', null, h('code', null, '{email}'), ' full address (john@example.com)'), h('li', null, h('code', null, '{user}'), ' local part (john)'), h('li', null, h('code', null, '{domain}'), ' domain (example.com)')),
        h('span', null, 'E.g. '), h('code', null, 'https://dav.example.com/{email}/calendar/')));
    s.tuning = s.tuning === 'manual' ? 'manual' : 'auto';
    const builders = h('input', { type: 'number', min: 0, max: 32, value: s.builders ?? '', placeholder: 'default', onInput: e => { s.builders = e.target.value === '' ? null : Number(e.target.value); } });
    const manualBox = h('div', { class: 'cols', hidden: s.tuning !== 'manual' },
      field('IMAP connections', I('workers', { type: 'number', min: 1, max: 32 })),
      field('Builder processes', builders, 'Empty = CPUs − 1.'));
    const segBtn = v => h('button', { type: 'button', class: s.tuning === v ? 'on' : '', onClick: () => setTuning(v) }, v === 'auto' ? 'Auto' : 'Manual');
    const tuneSeg = h('div', { class: 'seg', role: 'group' }, segBtn('auto'), segBtn('manual'));
    function setTuning(v) {
      s.tuning = v; manualBox.hidden = v !== 'manual';
      tuneSeg.querySelectorAll('button').forEach((b, i) => b.classList.toggle('on', ['auto', 'manual'][i] === v));
    }
    const body = h('form', { class: 'form', onSubmit: e => e.preventDefault() },
      field('Name', I('name', { required: true, placeholder: 'Production' })),
      h('div', { class: 'cols' }, field('IMAP host', I('imap_host', { required: true, placeholder: 'imap.example.com' })), field('Port', port)),
      h('div', { class: 'cols' }, field('Security', sec), field('Tuning', tuneSeg)), manualBox,
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: s.verify_tls, onChange: e => s.verify_tls = e.target.checked }), 'Verify TLS certificate'),
      field('IMAP root folder', I('root_folder', { placeholder: 'e.g. PST Import (empty = mailbox root)' }), 'PST folders will be created under this folder.'),
      h('label', { class: 'check' }, dav, 'Enable CalDAV / CardDAV (calendar and contacts)'), davBox,
      field('Notes', h('textarea', { rows: 2, onInput: e => s.notes = e.target.value }, s.notes || '')));
    modal({ title: isNew ? 'New server' : 'Edit server', body, wide: true, actions: [
      { label: 'Cancel', onClick: c => c() },
      { label: 'Save', kind: 'primary', ico: 'check', onClick: async c => {
        if (!body.reportValidity()) return;
        const payload = { ...s, root_folder: s.root_folder || null, caldav_url: s.caldav_url || null, carddav_url: s.carddav_url || null, workers: s.workers || 1, tuning: s.tuning, builders: s.builders ?? null };
        delete payload.id;
        try { isNew ? await post('/servers', payload) : await put('/servers/' + s0.id, payload); toast('Server saved.', 'ok'); c(); reload(); }
        catch (e) { toast(e.message, 'bad'); } } }] });
  }
  draw();
}
