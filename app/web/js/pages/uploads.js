import { get, del, on } from '../api.js';
import { h, clear, btn, icon, toast, confirmBox, empty, pstBadge, progressBar, pct, fmtBytes, fmtDur, fmtNum, modal } from '../ui.js';
import { addFile, tasks, pause, resume, cancel, dismiss, pendingResumables, forgetResumable } from '../uploader.js';

const KIND = { mail: 'Mail', contacts: 'Contacts', calendar: 'Calendar', tasks: 'Tasks', notes: 'Notes', journal: 'Journal', other: 'Other' };
const ST = { starting: 'Starting…', hashing: 'Hashing…', uploading: 'Uploading', paused: 'Paused', error: 'Interrupted', completing: 'Finalizing…', done: 'Done' };

export default async function (root) {
  let psts = await get('/psts');
  const input = h('input', { type: 'file', multiple: true, accept: '.pst,.ost', hidden: true, onChange: () => { take(input.files); input.value = ''; } });
  const resumeInput = h('input', { type: 'file', hidden: true });
  const zone = h('div', { class: 'dropzone', tabindex: 0, role: 'button', onClick: () => input.click(), onKeydown: e => { if (e.key === 'Enter') input.click(); },
    onDragover: e => { e.preventDefault(); zone.classList.add('over'); }, onDragleave: () => zone.classList.remove('over'),
    onDrop: e => { e.preventDefault(); zone.classList.remove('over'); take(e.dataTransfer.files); } },
    icon('upload', 34), h('b', null, 'Drop your PST files here'), h('span', null, 'or click to browse. Chunked upload, resumable after a network drop or page refresh.'), input);
  const activeBox = h('div', { class: 'stack' }), resBox = h('div'), listBox = h('div', { class: 'stack' });
  root.append(h('div', { class: 'page-h' }, h('h1', null, 'PST files'), h('p', { class: 'muted' }, 'Upload, validation and analysis of archives.')),
    zone, resBox, activeBox, h('h2', null, 'Files on the server'), listBox, resumeInput);

  function take(files) { for (const f of files) { if (!/\.(pst|ost)$/i.test(f.name) && !confirm(`"${f.name}" does not have a .pst extension. Upload anyway?`)) continue; addFile(f); } }

  function drawTasks() {
    clear(activeBox);
    for (const t of tasks.values()) {
      const p = pct(t.sentBytes, t.size), live = t.status === 'uploading';
      activeBox.append(h('div', { class: 'card up-card' },
        h('div', { class: 'row between' }, h('div', { class: 'ellipsis' }, icon('file', 16), ' ', h('b', null, t.filename), h('span', { class: 'muted' }, ' · ' + fmtBytes(t.size))),
          h('span', { class: 'badge ' + ({ error: 'bad', paused: 'warn', done: 'ok' }[t.status] || 'info') }, h('i'), ST[t.status] || t.status)),
        progressBar(t.status === 'done' ? 100 : p, { label: p + '%', striped: live, kind: t.status === 'error' ? 'bad' : t.status === 'done' ? 'ok' : '' }),
        h('div', { class: 'row between small muted' },
          h('span', null, `${fmtBytes(t.sentBytes)} / ${fmtBytes(t.size)} · chunk ${t.received.size}/${t.total || '?'}`, t.resumedFrom ? ` · resumed at chunk ${t.resumedFrom}` : ''),
          h('span', null, live ? `${fmtBytes(t.rate)}/s · ${fmtDur(t.eta)} left` : '')),
        t.warn ? h('div', { class: 'small warn-t' }, t.warn) : null, t.error ? h('div', { class: 'small err' }, t.error) : null,
        h('div', { class: 'row gap' }, live ? btn('Pause', { ico: 'pause', onClick: () => pause(t) }) : null,
          ['paused', 'error'].includes(t.status) ? btn('Resume', { ico: 'play', kind: 'primary', onClick: () => resume(t) }) : null,
          t.status === 'done' ? btn('Dismiss', { onClick: () => dismiss(t) }) : btn('Cancel', { kind: 'danger-ghost', ico: 'x', onClick: () => cancel(t) }))));
    }
    // interrupted uploads known to the browser: the user re-selects the file
    clear(resBox);
    const pend = pendingResumables();
    if (pend.length) resBox.append(h('div', { class: 'card resume' }, h('b', null, 'Uploads to resume'),
      h('p', { class: 'muted small' }, 'Re-select the same file (or drop it above): the upload will resume where it stopped.'),
      pend.map(r => h('div', { class: 'row between res-row' }, h('span', { class: 'ellipsis' }, icon('file', 16), ' ', h('b', null, r.filename), h('span', { class: 'muted' }, ` · ${fmtBytes(r.size)}`)),
        h('span', { class: 'row gap' }, btn('Select file', { kind: 'primary', onClick: () => {
          resumeInput.onchange = () => { const f = resumeInput.files[0]; resumeInput.value = ''; if (!f) return;
            if (`${f.name}|${f.size}|${f.lastModified}` !== r.key) toast('This file does not exactly match (name, size, date): a new upload will be created.', 'warn', 7000);
            addFile(f); };
          resumeInput.click(); } }), btn('', { ico: 'x', title: 'Forget', onClick: () => forgetResumable(r.key) }))))));
  }
  function drawList() {
    clear(listBox);
    const sorted = [...psts].sort((a, b) => (b.id > a.id ? 1 : -1));
    if (!sorted.length) return listBox.append(empty('No PST files', 'Uploaded files will appear here.'));
    for (const p of sorted) {
      const tot = p.scan?.totals;
      listBox.append(h('div', { class: 'card pst-card' },
        h('div', { class: 'row between' }, h('div', { class: 'ellipsis' }, icon('file', 16), ' ', h('b', null, p.filename), h('span', { class: 'muted' }, ' · ' + fmtBytes(p.size))), pstBadge(p.status)),
        p.status === 'validating' ? progressBar(100, { striped: true, label: 'Analyzing PST…' }) : null,
        p.status === 'uploading' && p.size ? h('div', { class: 'muted small' }, 'Upload incomplete. Resume it from the list above or re-select the file.') : null,
        p.error ? h('div', { class: 'small err' }, p.error) : null,
        tot ? h('div', { class: 'chips' }, [['mail', 'Mail'], ['calendar', 'Calendar'], ['contacts', 'Contacts']].map(([k, l]) => h('span', { class: 'chip' }, h('b', null, fmtNum(tot[k] ?? 0)), ' ' + l))) : null,
        h('div', { class: 'row gap' }, p.scan?.folders?.length ? btn('Folder details', { ico: 'folder', onClick: () => detail(p) }) : null, h('span', { class: 'sp' }),
          p.status === 'valid' ? h('a', { class: 'btn', href: `/api/psts/${p.id}/preview?limit=20`, download: '', title: 'Download a sample (.eml/.ics/.vcf) to check accents and attachments' }, icon('download', 16), h('span', null, 'Preview (.zip)')) : null,
          btn('', { ico: 'trash', kind: 'danger-ghost', title: 'Delete', onClick: async () => {
            if (!await confirmBox('Delete PST', `Delete "${p.filename}" from the server?`, { okLabel: 'Delete', danger: true })) return;
            try { await del('/psts/' + p.id); psts = psts.filter(x => x.id !== p.id); drawList(); toast('PST deleted.', 'ok'); } catch (e) { toast(e.message, 'bad'); } } }))));
    }
  }
  function detail(p) {
    const rows = p.scan.folders;
    modal({ title: p.filename, wide: true, body: h('div', null,
      h('table', { class: 'tbl small' }, h('thead', null, h('tr', null, ['Folder', 'Type', 'Items'].map(t => h('th', null, t)))),
        h('tbody', null, rows.map(f => h('tr', null, h('td', null, Array.isArray(f.path) ? f.path.join(' / ') : f.path), h('td', null, KIND[f.kind] || f.kind), h('td', { class: 'num' }, fmtNum(f.count)))))),
      p.scan.errors?.length ? h('div', null, h('h4', null, 'Scan warnings'), h('ul', null, p.scan.errors.slice(0, 50).map(e => h('li', { class: 'small err' }, typeof e === 'string' ? e : JSON.stringify(e)))) ) : null),
      actions: [{ label: 'Close', onClick: c => c() }] });
  }

  drawTasks(); drawList();
  let tm; const soon = () => { clearTimeout(tm); tm = setTimeout(drawTasks, 150); };
  const offs = [on('uploader', soon), on('pst', p => { const i = psts.findIndex(x => x.id === p.id); i < 0 ? psts.push(p) : psts[i] = p; drawList(); }),
    on('upload', async () => { psts = await get('/psts'); drawList(); })];
  return () => { clearTimeout(tm); offs.forEach(f => f()); };
}
