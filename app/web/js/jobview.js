import { h, jobBadge, progressBar, pct, fmtNum, fmtDur, fmtBytes, PHASES } from './ui.js';

export const KINDS = [['mail', 'Mails'], ['calendar', 'Agenda'], ['contacts', 'Contacts']];
export function totals(job) {
  let done = 0, total = 0, errors = 0;
  for (const [k] of KINDS) { const p = job.progress?.[k]; if (p) { done += p.done || 0; total += p.total || 0; errors += p.errors || 0; } }
  return { done, total, errors, pct: pct(done, total) };
}
export function phaseBars(job) {
  return h('div', { class: 'phases' }, KINDS.map(([k, label]) => {
    const p = job.progress?.[k]; if (!p || (!p.total && !p.done)) return null;
    const active = job.phase === k && job.status === 'running';
    return h('div', { class: 'phase' + (active ? ' active' : '') },
      h('div', { class: 'ph-h' }, h('b', null, label), h('span', null, `${fmtNum(p.done)} / ${fmtNum(p.total)}`, p.errors ? h('em', { class: 'err' }, ` · ${p.errors} err.`) : null)),
      progressBar(pct(p.done, p.total), { kind: p.errors ? 'warn' : (active ? '' : 'ok'), striped: active }));
  }));
}
export function jobCard(job, email) {
  const t = totals(job);
  return h('a', { class: 'card job-card', href: '#/jobs/' + job.id },
    h('div', { class: 'row between' }, h('b', { class: 'ellipsis' }, email || ('Compte #' + job.account_id)), jobBadge(job.status)),
    h('div', { class: 'muted small' }, PHASES[job.phase] || job.phase || '', job.current_folder ? ' · ' + job.current_folder : ''),
    progressBar(t.pct, { label: t.pct + ' %', striped: job.status === 'running', kind: t.errors ? 'warn' : '' }),
    h('div', { class: 'row between small muted' }, h('span', null, `${fmtNum(t.done)} / ${fmtNum(t.total)} éléments`),
      h('span', null, job.status === 'running' ? `${job.rate ? Number(job.rate).toFixed(1).replace('.', ',') : '0'}/s · ${fmtDur(job.eta)}` : '')));
}
