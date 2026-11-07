import { describe, expect, it } from 'vitest';
import type { Account, Job, Pst } from '../../src/lib/api/types.ts';
import { connectionCounts, connectionOf, currentResult, importPlan, jobPercent, matchesFilter, overallProgress, rowRank, summarize, testPassed } from '../../src/lib/logic/accounts.ts';
import { createCoalescer } from '../../src/lib/logic/coalesce.ts';
import { maskEmail, maskFile, maskHost, maskText } from '../../src/lib/logic/mask.ts';
import { legacyRedirect, matchRoute, rowHref } from '../../src/lib/logic/routes.ts';
import { headerState, splitSelection, toggleAllVisible } from '../../src/lib/logic/selection.ts';
import { createTestRunner } from '../../src/lib/logic/testrun.ts';
import { VIRTUAL_AT, windowRange } from '../../src/lib/logic/virtual.ts';

const tick = () => new Promise((r) => setTimeout(r, 0));

describe('selection', () => {
	it('the header reflects single-row changes', () => {
		const selected = new Set<number>();
		expect(headerState([1, 2], selected)).toBe('none');
		selected.add(1);
		expect(headerState([1, 2], selected)).toBe('some');
		selected.add(2);
		expect(headerState([1, 2], selected)).toBe('all');
		selected.delete(1);
		expect(headerState([1, 2], selected)).toBe('some');
	});

	it('the header only looks at what is visible', () => {
		expect(headerState([1], new Set([1, 2]))).toBe('all');
		expect(headerState([3], new Set([1, 2]))).toBe('none');
		expect(headerState([], new Set([1]))).toBe('none');
	});

	it('hidden rows are counted apart and never targeted', () => {
		const { targets, hidden } = splitSelection(new Set([1, 2, 3]), [2, 4]);
		expect(targets).toEqual([2]);
		expect(hidden).toEqual([1, 3]);
	});

	it('select all adds and clears visible rows only', () => {
		const selected = new Set<number>([9]);
		toggleAllVisible('none', [1, 2], selected);
		expect([...selected].sort()).toEqual([1, 2, 9]);
		toggleAllVisible('all', [1, 2], selected);
		expect([...selected]).toEqual([9]);
		selected.add(1);
		toggleAllVisible('some', [1, 2], selected);
		expect(selected.has(2)).toBe(true);
	});
});

describe('test runner', () => {
	it('a second trigger is ignored while waiting or running, and the limit holds', async () => {
		const running: number[] = [];
		let peak = 0;
		const done: (() => void)[] = [];
		const seen: (string | undefined)[] = [];
		const summaries: number[] = [];
		const runner = createTestRunner<{ imap: { ok: boolean; msg: string } }>({
			concurrency: 2,
			run: (id) => {
				running.push(id);
				peak = Math.max(peak, running.length);
				return new Promise((resolve) => {
					done.push(() => {
						running.splice(running.indexOf(id), 1);
						resolve({ imap: { ok: true, msg: '' } });
					});
				});
			},
			onPhase: (id, phase) => {
				if (id === 3) seen.push(phase);
			},
			onBatchDone: (s) => summaries.push(s.total)
		});
		expect(runner.enqueue([1, 2, 3])).toEqual([1, 2, 3]);
		expect(runner.enqueue([1, 3, 4])).toEqual([4]);
		expect(runner.phase(1)).toBe('running');
		expect(runner.phase(3)).toBe('waiting');
		while (done.length) {
			done.shift()?.();
			await tick();
		}
		expect(peak).toBe(2);
		expect(seen).toEqual(['waiting', 'running', undefined]);
		expect(summaries).toEqual([4]);
		expect(runner.pending).toBe(0);
		expect(runner.enqueue([1])).toEqual([1]);
	});

	it('a failed test is reported in the summary and frees the account', async () => {
		const results: unknown[] = [];
		const runner = createTestRunner<string>({
			run: async () => {
				throw new Error('boom');
			},
			onBatchDone: (s) => results.push(s.results.get(7))
		});
		runner.enqueue([7]);
		await tick();
		await tick();
		expect((results[0] as Error).message).toBe('boom');
		expect(runner.phase(7)).toBeUndefined();
	});
});

describe('live events', () => {
	it('keeps the latest per object and flushes once', () => {
		let plan: (() => void) | null = null;
		let schedules = 0;
		const batches: string[][] = [];
		const c = createCoalescer<string>(
			(run) => {
				schedules++;
				plan = run;
			},
			(batch) => batches.push(batch)
		);
		for (let i = 0; i < 1000; i++) c.push('job:1', `one-${i}`);
		c.push('job:2', 'two');
		expect(schedules).toBe(1);
		expect(c.size).toBe(2);
		(plan as unknown as () => void)();
		expect(batches).toEqual([['one-999', 'two']]);
		c.push('job:1', 'again');
		expect(schedules).toBe(2);
	});
});

describe('virtual window', () => {
	it('renders everything under the threshold', () => {
		expect(windowRange(VIRTUAL_AT, 5000, 800, 52)).toEqual({ start: 0, end: VIRTUAL_AT, before: 0, after: 0 });
	});

	it('renders only what shows, and the spacers keep the height', () => {
		const r = windowRange(5000, 52 * 1000, 800, 52);
		expect(r.end - r.start).toBeLessThan(60);
		expect(r.start).toBeLessThanOrEqual(1000);
		expect(r.end).toBeGreaterThan(1000 + 15);
		expect(r.before + (r.end - r.start) * 52 + r.after).toBe(5000 * 52);
	});

	it('stays in bounds at both ends', () => {
		expect(windowRange(1000, -500, 800, 50).start).toBe(0);
		const end = windowRange(1000, 10_000_000, 800, 50);
		expect(end.end).toBe(1000);
		expect(end.start).toBeLessThan(1000);
		expect(end.after).toBe(0);
	});
});

describe('routes', () => {
	it('a row keeps the filters and opens the job, or the mailbox before there is one', () => {
		expect(rowHref('', 3, undefined)).toBe('/?account=3');
		expect(rowHref('?q=al&status=done', 3, null)).toBe('/?q=al&status=done&account=3');
		expect(rowHref('', 3, 7)).toBe('/jobs/7');
		expect(rowHref('?q=al&tab=errors&account=2', 3, 7)).toBe('/jobs/7?q=al');
	});

	it('three pages, the job panel on the first', () => {
		expect(matchRoute('/')).toEqual({ page: 'migrations', jobId: null });
		expect(matchRoute('/jobs/12')).toEqual({ page: 'migrations', jobId: 12 });
		expect(matchRoute('/files/')).toEqual({ page: 'files', jobId: null });
		expect(matchRoute('/settings')).toEqual({ page: 'settings', jobId: null });
		expect(matchRoute('/nope')).toEqual({ page: 'migrations', jobId: null });
	});

	it.each([
		['/', '#/dashboard', '/'],
		['/', '#/accounts?server=2', '/?server=2'],
		['/', '#/servers', '/servers'],
		['/', '#/uploads', '/files'],
		['/', '#/settings', '/settings'],
		['/', '#/jobs/12', '/jobs/12'],
		['/', '#/', '/'],
		['/dashboard', '', '/'],
		['/uploads', '', '/files']
	])('old link %s%s goes to %s', (path, hash, target) => {
		expect(legacyRedirect(path, hash)).toBe(target);
	});

	it('current URLs are left alone', () => {
		expect(legacyRedirect('/', '')).toBeNull();
		expect(legacyRedirect('/jobs/3', '')).toBeNull();
		expect(legacyRedirect('/settings', '#servers')).toBeNull();
	});
});

const account = (over: Partial<Account> = {}): Account => ({
	id: 1,
	server_id: 1,
	email: 'alice@example.com',
	login: 'alice@example.com',
	has_password: true,
	status: 'new',
	last_test: null,
	last_test_result: null,
	pst_id: null,
	options: { mail: true, calendar: true, contacts: true },
	job: null,
	...over
});
const job = (status: Job['status']) => ({ status }) as Job;

describe('accounts', () => {
	it('filters', () => {
		const a = account({ pst_id: 3, status: 'ok' });
		const none = { q: '', server: null, status: '' as const };
		expect(matchesFilter(a, undefined, none)).toBe(true);
		expect(matchesFilter(a, undefined, { ...none, q: 'bob' })).toBe(false);
		expect(matchesFilter(a, undefined, { ...none, server: 2 })).toBe(false);
		expect(matchesFilter(a, undefined, { ...none, status: 'no_pst' })).toBe(false);
		expect(matchesFilter(a, job('running'), { ...none, status: 'active' })).toBe(true);
		expect(matchesFilter(a, job('done_with_errors'), { ...none, status: 'problem' })).toBe(true);
	});

	it('a stale result does not count once the password changed', () => {
		const result = { imap: { ok: true, msg: 'ok' } };
		expect(currentResult(account({ status: 'ok', last_test_result: result }))).toBe(result);
		expect(currentResult(account({ status: 'new', last_test_result: result }))).toBeNull();
	});

	it('a test passes only when every protocol passes', () => {
		expect(testPassed({ imap: { ok: true, msg: '' }, caldav: { ok: false, msg: 'x' } })).toBe(false);
		expect(testPassed({ imap: { ok: true, msg: '' }, caldav: { ok: true, msg: '' } })).toBe(true);
	});

	it('summarizes', () => {
		const list = [account({ id: 1 }), account({ id: 2, status: 'auth_failed' }), account({ id: 3 })];
		const jobs: Record<number, Job> = { 1: job('running'), 3: job('done') };
		expect(summarize(list, (id) => jobs[id])).toEqual({ total: 3, running: 1, done: 1, attention: 1 });
	});

	it('orders what needs a person first and finished work last', () => {
		const a = account({ id: 1 });
		expect(rowRank(account({ id: 2, status: 'auth_failed' }), undefined)).toBe(0);
		expect(rowRank(a, job('failed'))).toBe(0);
		expect(rowRank(a, job('done_with_errors'))).toBe(0);
		expect(rowRank(a, job('running'))).toBe(1);
		expect(rowRank(a, job('queued'))).toBe(1);
		expect(rowRank(a, undefined)).toBe(2);
		expect(rowRank(a, job('stopped'))).toBe(2);
		expect(rowRank(a, job('done'))).toBe(3);
	});

	it('a job percent counts every stream, and a job without progress is at zero', () => {
		const stream = (done: number, total: number) => ({ done, total, errors: 0 });
		expect(jobPercent(undefined)).toBe(0);
		expect(jobPercent({ progress: { mail: stream(1, 4), calendar: stream(1, 1), contacts: stream(0, 0) } })).toBe(40);
	});

	it('overall progress is the mean of the mailboxes, the time left only while something moves', () => {
		const stream = (done: number, total: number) => ({ done, total, errors: 0 });
		const running = { ...job('running'), rate: 10, progress: { mail: stream(50, 100), calendar: stream(0, 0), contacts: stream(0, 0) } };
		const list = [account({ id: 1 }), account({ id: 2 }), account({ id: 3 }), account({ id: 4 })];
		const jobs: Record<number, Job> = { 1: running, 2: job('done') };
		expect(overallProgress(list, (id) => jobs[id])).toEqual({ percent: 38, left: 5 });
		expect(overallProgress(list, (id) => (id === 2 ? job('done') : undefined))).toEqual({ percent: 25, left: null });
		expect(overallProgress([], () => undefined)).toEqual({ percent: 0, left: null });
	});

	it('the import plan says what will not start and why', () => {
		const valid = { id: 1, status: 'valid' } as Pst;
		const invalid = { id: 2, status: 'invalid' } as Pst;
		const list = [
			account({ id: 1, pst_id: 1 }),
			account({ id: 2 }),
			account({ id: 3, pst_id: 2 }),
			account({ id: 4, pst_id: 1, has_password: false }),
			account({ id: 5, pst_id: 1 })
		];
		const plan = importPlan(list, (id) => (id === 5 ? job('queued') : undefined), (id) => (id === 1 ? valid : invalid));
		expect(plan.ready.map((a) => a.id)).toEqual([1]);
		expect(plan.noPst.map((a) => a.id)).toEqual([2, 3]);
		expect(plan.noPassword.map((a) => a.id)).toEqual([4]);
		expect(plan.active.map((a) => a.id)).toEqual([5]);
	});
});

describe('mask', () => {
	it('hides addresses, hosts and file names', () => {
		expect(maskEmail('alice@example.com')).not.toContain('example');
		expect(maskEmail('alice@example.com').endsWith('.com')).toBe(true);
		expect(maskHost('imap.example.com')).not.toContain('example');
		expect(maskHost('192.0.2.5')).not.toContain('192');
		expect(maskFile('alice-archive.pst').endsWith('.pst')).toBe(true);
		expect(maskFile('alice-archive.pst')).not.toContain('archive');
	});

	it('finds them in free text', () => {
		const out = maskText('login alice@example.com to https://dav.example.com/x at 192.0.2.5 failed');
		expect(out).not.toContain('alice@');
		expect(out).not.toContain('dav.example');
		expect(out).not.toContain('192.0.2.5');
		expect(out).toContain('failed');
	});
});

describe('connection at a glance', () => {
	it('reads the last test as verified, failed or untested, and counts them', () => {
		expect(connectionOf({ status: 'ok' })).toBe('ok');
		expect(connectionOf({ status: 'new' })).toBe('untested');
		expect(connectionOf({ status: 'auth_failed' })).toBe('failed');
		expect(connectionOf({ status: 'error' })).toBe('failed');
		const list = [{ status: 'ok' }, { status: 'ok' }, { status: 'auth_failed' }, { status: 'new' }] as const;
		expect(connectionCounts(list)).toEqual({ ok: 2, failed: 1, untested: 1 });
	});
});
