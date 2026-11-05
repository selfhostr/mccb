import { get, post, put, del } from '../api.js';
import { h, clear, btn, icon, modal, field, toast, confirmBox, empty } from '../ui.js';

const SEC = { ssl: 'SSL/TLS implicite', starttls: 'STARTTLS', none: 'Aucun (clair)' };
const blank = () => ({ name: '', imap_host: '', imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: '', caldav_url: '', carddav_url: '', dav_enabled: false, workers: 2, notes: '' });

export default async function (root) {
  let servers = await get('/servers');
  const list = h('div', { class: 'grid cards' });
  root.append(h('div', { class: 'page-h row between' }, h('div', null, h('h1', null, 'Serveurs'), h('p', { class: 'muted' }, 'Destinations IMAP / CalDAV / CardDAV des migrations.')),
    btn('Nouveau serveur', { ico: 'plus', kind: 'primary', onClick: () => edit(blank()) })), list);

  async function reload() { servers = await get('/servers'); draw(); }
  function draw() {
    clear(list);
    if (!servers.length) return list.append(empty('Aucun serveur', 'Ajoutez un premier serveur pour pouvoir créer des comptes.'));
    for (const s of servers) list.append(h('div', { class: 'card server' },
      h('div', { class: 'row between' }, h('b', null, s.name), h('span', { class: 'badge ' + (s.dav_enabled ? 'ok' : 'muted') }, h('i'), s.dav_enabled ? 'DAV activé' : 'IMAP seul')),
      h('dl', { class: 'kv' }, h('dt', null, 'IMAP'), h('dd', null, `${s.imap_host}:${s.imap_port} · ${SEC[s.imap_security] || s.imap_security}${s.verify_tls ? '' : ' · TLS non vérifié'}`),
        s.root_folder ? [h('dt', null, 'Dossier racine'), h('dd', null, s.root_folder)] : null,
        s.dav_enabled ? [h('dt', null, 'CalDAV'), h('dd', { class: 'mono' }, s.caldav_url || 'auto-découverte'), h('dt', null, 'CardDAV'), h('dd', { class: 'mono' }, s.carddav_url || 'auto-découverte')] : null,
        h('dt', null, 'Workers'), h('dd', null, s.workers)),
      s.notes ? h('p', { class: 'muted small' }, s.notes) : null,
      h('div', { class: 'row gap actions' }, btn('Tester', { ico: 'test', onClick: () => test(s) }), btn('Comptes', { ico: 'users', onClick: () => location.hash = '#/accounts?server=' + s.id }),
        h('span', { class: 'sp' }), btn('', { ico: 'edit', title: 'Modifier', onClick: () => edit(s) }),
        btn('', { ico: 'trash', kind: 'danger-ghost', title: 'Supprimer', onClick: () => remove(s) }))));
  }
  async function test(s) {
    toast(`Test de « ${s.name} »…`, 'info', 1500);
    try {
      const r = await post(`/servers/${s.id}/test`);
      const row = (n, x) => x ? h('li', { class: x.ok ? 'ok' : 'bad' }, h('b', null, n), ' ', x.ok ? '✔' : '✘', ' ', x.msg || '') : null;
      modal({ title: `Test de ${s.name}`, body: h('ul', { class: 'testlist' }, row('IMAP', r.imap), row('CalDAV', r.caldav), row('CardDAV', r.carddav),
        (!r.imap && r.ok != null) ? h('li', { class: r.ok ? 'ok' : 'bad' }, r.msg || (r.ok ? 'OK' : 'Échec')) : null), actions: [{ label: 'Fermer', onClick: c => c() }] });
    } catch (e) { toast(e.message, 'bad'); }
  }
  async function remove(s) {
    if (!await confirmBox('Supprimer le serveur', `Supprimer « ${s.name} » ? Les comptes associés seront également supprimés.`, { okLabel: 'Supprimer', danger: true })) return;
    try { await del('/servers/' + s.id); toast('Serveur supprimé.', 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
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
      field('URL CalDAV', I('caldav_url', { placeholder: 'vide = auto-découverte (.well-known)' })),
      field('URL CardDAV', I('carddav_url', { placeholder: 'vide = auto-découverte (.well-known)' })),
      h('div', { class: 'help' }, h('b', null, 'Gabarits disponibles'), h('ul', null,
        h('li', null, h('code', null, '{email}'), ' adresse complète (jean@exemple.fr)'), h('li', null, h('code', null, '{user}'), ' partie locale (jean)'), h('li', null, h('code', null, '{domain}'), ' domaine (exemple.fr)')),
        h('span', null, 'Ex. : '), h('code', null, 'https://dav.exemple.fr/{email}/calendar/')));
    const body = h('form', { class: 'form', onSubmit: e => e.preventDefault() },
      field('Nom', I('name', { required: true, placeholder: 'Production OVH' })),
      h('div', { class: 'cols' }, field('Hôte IMAP', I('imap_host', { required: true, placeholder: 'imap.exemple.fr' })), field('Port', port)),
      h('div', { class: 'cols' }, field('Sécurité', sec), field('Workers parallèles', I('workers', { type: 'number', min: 1, max: 16 }))),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: s.verify_tls, onChange: e => s.verify_tls = e.target.checked }), 'Vérifier le certificat TLS'),
      field('Dossier racine IMAP', I('root_folder', { placeholder: 'ex. Import PST (vide = racine de la boîte)' }), 'Les dossiers du PST seront créés sous ce dossier.'),
      h('label', { class: 'check' }, dav, 'Activer CalDAV / CardDAV (agenda et contacts)'), davBox,
      field('Notes', h('textarea', { rows: 2, onInput: e => s.notes = e.target.value }, s.notes || '')));
    modal({ title: isNew ? 'Nouveau serveur' : 'Modifier le serveur', body, wide: true, actions: [
      { label: 'Annuler', onClick: c => c() },
      { label: 'Enregistrer', kind: 'primary', ico: 'check', onClick: async c => {
        if (!body.reportValidity()) return;
        const payload = { ...s, root_folder: s.root_folder || null, caldav_url: s.caldav_url || null, carddav_url: s.carddav_url || null, workers: s.workers || 1 };
        delete payload.id;
        try { isNew ? await post('/servers', payload) : await put('/servers/' + s0.id, payload); toast('Serveur enregistré.', 'ok'); c(); reload(); }
        catch (e) { toast(e.message, 'bad'); } } }] });
  }
  draw();
}
