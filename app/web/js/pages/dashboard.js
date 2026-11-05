import { get, on } from '../api.js';
import { h, clear, empty, isActive, fmtBytes, fmtNum } from '../ui.js';
import { jobCard } from '../jobview.js';
import { tasks } from '../uploader.js';

export default async function (root) {
  const [servers, accounts, psts, jobs] = await Promise.all([get('/servers'), get('/accounts'), get('/psts'), get('/jobs?active=1')]);
  const emails = new Map(accounts.map(a => [a.id, a.email]));
  const J = new Map(jobs.map(j => [j.id, j]));
  const stats = h('div', { class: 'stats' });
  const list = h('div', { class: 'grid cards' });
  root.append(h('div', { class: 'page-h' }, h('h1', null, 'Dashboard'), h('p', { class: 'muted' }, 'Overview of ongoing migrations.')),
    stats, h('h2', null, 'Active jobs'), list);

  function stat(label, value, sub, cls = '') { return h('div', { class: 'stat ' + cls }, h('small', null, label), h('b', null, value), h('span', null, sub || '')); }
  function draw() {
    const act = [...J.values()].filter(j => isActive(j.status));
    const run = act.filter(j => j.status === 'running').length;
    const valid = psts.filter(p => p.status === 'valid');
    clear(stats).append(
      stat('Active jobs', fmtNum(act.length), `${run} running · ${act.length - run} queued/paused`, ''),
      stat('Servers', fmtNum(servers.length), 'configured destinations'),
      stat('Accounts', fmtNum(accounts.length), `${accounts.filter(a => a.pst_id).length} with a PST assigned`),
      stat('PST files', fmtNum(psts.length), `${valid.length} valid · ${fmtBytes(psts.reduce((s, p) => s + (p.size || 0), 0))}`));
    clear(list);
    if (!act.length) list.append(empty('No active jobs', 'Select accounts under Accounts, then start an import.'));
    else act.sort((a, b) => a.id - b.id).forEach(j => list.append(jobCard(j, emails.get(j.account_id))));
  }
  draw();
  const offs = [on('job', j => { J.set(j.id, j); draw(); }),
    on('pst', p => { const i = psts.findIndex(x => x.id === p.id); i < 0 ? psts.push(p) : psts[i] = p; draw(); }),
    on('account', a => { const i = accounts.findIndex(x => x.id === a.id); i < 0 ? accounts.push(a) : accounts[i] = a; emails.set(a.id, a.email); draw(); })];
  return () => offs.forEach(f => f());
}
