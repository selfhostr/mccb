import { get, post, on, api } from '../api.js';
import { h, clear, btn, icon, toast, confirmBox, jobBadge, progressBar, pct, fmtDate, fmtDur, fmtNum, fmtBytes, isActive, PHASES, empty, download } from '../ui.js';
import { phaseBars, totals, KINDS } from '../jobview.js';

const ORDER = ['precheck', 'mail', 'calendar', 'contacts', 'verify'];

export default async function (root, { params }) {
  const id = Number(params[0]);
  let job = await get('/jobs/' + id), account = null, tab = 'log';
  try { account = (await get('/accounts')).find(a => a.id === job.account_id); } catch {}

  const head = h('div'), body = h('div'), tabs = h('div', { class: 'tabs' }), pane = h('div');
  root.append(h('a', { href: '#/dashboard', class: 'back' }, '← Dashboard'), head, body, tabs, pane);

  const act = async (what, label) => { try { job = await post(`/jobs/${id}/${what}`) || job; toast(label, 'ok'); drawTop(); } catch (e) { toast(e.message, 'bad'); } };
  function drawTop() {
    const t = totals(job), run = job.status === 'running', st = job.status;
    clear(head).append(h('div', { class: 'page-h row between wrap' },
      h('div', null, h('h1', null, account?.email || ('Job #' + id)), h('div', { class: 'row gap' }, jobBadge(st), h('span', { class: 'muted small' }, `Job #${id}`))),
      h('div', { class: 'row gap wrap' },
        run ? btn('Pause', { ico: 'pause', onClick: () => act('pause', 'Pause requested.') }) : null,
        st === 'paused' ? btn('Resume', { ico: 'play', kind: 'primary', onClick: () => act('resume', 'Resumed.') }) : null,
        ['running', 'paused', 'queued'].includes(st) ? btn('Stop', { ico: 'stop', kind: 'danger', onClick: async () => { if (await confirmBox('Stop job', 'The job will stop cleanly. You can restart it without duplicates.', { okLabel: 'Stop', danger: true })) act('stop', 'Stop requested.'); } }) : null,
        ['stopped', 'failed', 'done_with_errors', 'done'].includes(st) ? btn('Retry', { ico: 'retry', onClick: () => act('retry', 'Job restarted.') }) : null,
        ['done_with_errors', 'stopped', 'failed'].includes(st) ? btn('Retry errors', { ico: 'retry', onClick: () => act('retry-errors', 'Retrying errors.') }) : null)));
    const steps = h('div', { class: 'steps' }, ORDER.map((p, i) => {
      const cur = ORDER.indexOf(job.phase), cls = isActive(st) || st === 'running' ? (i < cur ? 'past' : i === cur ? 'cur' : '') : (['done', 'done_with_errors'].includes(st) ? 'past' : i < cur ? 'past' : i === cur ? 'cur' : '');
      return h('div', { class: 'step ' + cls }, h('i', null, cls === 'past' ? '✓' : i + 1), h('span', null, PHASES[p]));
    }));
    clear(body).append(
      h('div', { class: 'card' }, steps,
        h('div', { class: 'big-prog' }, progressBar(t.pct, { label: `${t.pct}%`, striped: run, kind: t.errors ? 'warn' : (st === 'done' ? 'ok' : '') })),
        h('div', { class: 'stats mini' },
          mini('Items', `${fmtNum(t.done)} / ${fmtNum(t.total)}`), mini('Errors', fmtNum(t.errors), t.errors ? 'bad' : ''),
          mini(run ? 'Rate' : 'Average rate', job.rate != null ? `${Number(job.rate).toFixed(1)} items/s` : '—'), mini('Time left', run ? fmtDur(job.eta) : '—'),
          mini('Started', fmtDate(job.started_at)), mini('Finished', fmtDate(job.finished_at))),
        job.current_folder ? h('div', { class: 'curfolder' }, icon('folder', 16), ' Current folder: ', h('b', null, job.current_folder)) : null,
        job.error ? h('div', { class: 'alert bad' }, icon('alert', 16), ' ', job.error) : null, phaseBars(job)));
  }
  const mini = (l, v, c = '') => h('div', { class: 'stat ' + c }, h('small', null, l), h('b', null, v));

  // ----- tabs
  const TABS = [['log', 'Live log'], ['errors', 'Errors'], ['report', 'Report']];
  function drawTabs() {
    clear(tabs).append(...TABS.map(([k, l]) => h('button', { class: k === tab ? 'on' : '', onClick: () => { tab = k; drawTabs(); showTab(); } }, l)));
  }
  let stopLog = null;
  function showTab() { stopLog?.(); stopLog = null; clear(pane); ({ log: logTab, errors: errorsTab, report: reportTab })[tab](); }

  const lines = [];
  function logTab() {
    const box = h('pre', { class: 'log', tabindex: 0 });
    let follow = true, after = null, alive = true;
    box.addEventListener('scroll', () => { follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 30; });
    const lvl = h('select', { onChange: () => { redraw(); } }, h('option', { value: '' }, 'All levels'), h('option', { value: 'warn' }, 'Warnings+'), h('option', { value: 'error' }, 'Errors'));
    pane.append(h('div', { class: 'toolbar' }, lvl, h('span', { class: 'sp' }), btn('Copy', { onClick: () => navigator.clipboard?.writeText(box.textContent) }), btn('Download', { ico: 'download', onClick: () => download(`job-${id}.log`, lines.map(fmtLine).join('\n'), 'text/plain') })), box);
    const norm = l => typeof l === 'string' ? { msg: l } : { ts: l.ts ?? l.time, level: (l.level || 'info').toLowerCase(), msg: l.msg ?? l.message ?? JSON.stringify(l), n: l.n ?? l.id ?? l.seq };
    const fmtLine = l => `${l.ts ? fmtDate(l.ts) + '  ' : ''}${l.level && l.level !== 'info' ? '[' + l.level.toUpperCase() + '] ' : ''}${l.msg}`;
    const ok = l => !lvl.value || (lvl.value === 'error' ? /err/.test(l.level) : /warn|err/.test(l.level));
    const el = l => h('div', { class: 'ln ' + (l.level || '') }, h('span', { class: 't' }, l.ts ? new Date(typeof l.ts === 'number' && l.ts < 1e12 ? l.ts * 1000 : l.ts).toLocaleTimeString('en-US') : ''), l.msg);
    const redraw = () => { clear(box).append(...lines.filter(ok).map(el)); box.scrollTop = box.scrollHeight; };
    async function poll() {
      while (alive) {
        try {
          const r = await get(`/jobs/${id}/log${after != null ? '?after=' + encodeURIComponent(after) : ''}`);
          const arr = (Array.isArray(r) ? r : (r.lines || r.log || [])).map(norm);
          if (arr.length) {
            after = (!Array.isArray(r) && (r.next ?? r.after)) ?? arr[arr.length - 1].n ?? ((after ?? 0) + arr.length);
            lines.push(...arr); if (lines.length > 5000) lines.splice(0, lines.length - 5000);
            box.append(...arr.filter(ok).map(el)); if (follow) box.scrollTop = box.scrollHeight;
          }
        } catch {}
        await new Promise(r => setTimeout(r, isActive(job.status) ? 1500 : 5000));
      }
    }
    lines.length = 0; poll();
    stopLog = () => { alive = false; };
  }

  async function errorsTab() {
    pane.append(h('div', { class: 'loading' }, 'Loading…'));
    try {
      const r = await get(`/jobs/${id}/errors`); const arr = Array.isArray(r) ? r : (r.errors || []);
      clear(pane);
      if (!arr.length) return pane.append(empty('No errors', 'Everything went fine so far.'));
      pane.append(h('div', { class: 'row between' }, h('b', null, `${arr.length} error(s)`), btn('Retry errors', { ico: 'retry', onClick: () => act('retry-errors', 'Retrying errors.') })),
        h('div', { class: 'table-wrap' }, h('table', { class: 'tbl small' }, h('thead', null, h('tr', null, ['Type', 'Folder', 'Item', 'Error'].map(t => h('th', null, t)))),
          h('tbody', null, arr.map(e => h('tr', null, h('td', null, e.kind || e.type || ''), h('td', null, e.folder || ''), h('td', { class: 'mono' }, e.subject || e.item_key || e.item || ''), h('td', { class: 'err' }, e.error || e.msg || e.message || String(e))))))));
    } catch (e) { clear(pane).append(h('p', { class: 'err' }, e.message)); }
  }

  async function reportTab() {
    pane.append(h('div', { class: 'loading' }, 'Loading…'));
    try {
      const r = await get(`/jobs/${id}/report`); clear(pane);
      const secs = KINDS.map(([k, l]) => [k, l, r[k] || r.totals?.[k] || r.summary?.[k]]).filter(x => x[2] && typeof x[2] === 'object');
      pane.append(h('div', { class: 'row between' }, h('b', null, 'Migration report'), btn('Download report (JSON)', { ico: 'download', onClick: () => download(`report-job-${id}.json`, JSON.stringify(r, null, 2)) })));
      if (secs.length) pane.append(h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Type', 'Sent', 'On server', 'Errors', 'Warnings', 'Skipped'].map(t => h('th', null, t)))),
        h('tbody', null, secs.map(([k, l, s]) => {
          const sent = s.sent ?? s.ok ?? s.done, pres = s.present ?? s.server, mismatch = pres != null && sent != null && pres < sent;
          return h('tr', null, h('td', null, h('b', null, l)), h('td', { class: 'num' }, fmtNum(sent)), h('td', { class: 'num ' + (mismatch ? 'err' : pres != null ? 'okt' : '') }, fmtNum(pres), mismatch ? ' ⚠' : ''),
            h('td', { class: 'num' }, fmtNum(s.errors ?? 0)), h('td', { class: 'num' }, fmtNum(s.warnings ?? 0)), h('td', { class: 'num' }, fmtNum(s.skipped ?? 0))); })))));
      if (Array.isArray(r.folders) && r.folders.length) pane.append(h('h4', null, 'By folder'), h('div', { class: 'table-wrap' }, h('table', { class: 'tbl small' },
        h('thead', null, h('tr', null, ['Folder', 'Sent', 'Present'].map(t => h('th', null, t)))), h('tbody', null, r.folders.map(f => h('tr', null, h('td', null, Array.isArray(f.path) ? f.path.join(' / ') : (f.path || f.name)), h('td', { class: 'num' }, fmtNum(f.sent)), h('td', { class: 'num ' + (f.present < f.sent ? 'err' : '') }, fmtNum(f.present))))))));
      if (r.sample_check) pane.append(h('p', { class: 'muted small' }, 'Sample check: ', typeof r.sample_check === 'string' ? r.sample_check : JSON.stringify(r.sample_check)));
      if (!secs.length) pane.append(h('pre', { class: 'log' }, JSON.stringify(r, null, 2)));
    } catch (e) { clear(pane).append(empty('Report unavailable', e.status === 404 ? 'It will be available when the job finishes.' : e.message)); }
  }

  drawTop(); drawTabs(); showTab();
  const off = on('job', j => { if (j.id !== id) return; const was = job.status; job = j; drawTop(); if (tab === 'report' && was !== j.status) showTab(); });
  const reconcile = on('sse-open', async () => { try { job = await get('/jobs/' + id); drawTop(); } catch {} });
  return () => { off(); reconcile(); stopLog?.(); };
}
