import { describe, expect, it } from 'vitest';
import type { Account, Job, JobStatus, Pst } from '../../src/lib/api/types.ts';
import { importPlan } from '../../src/lib/logic/accounts.ts';
import { BULK_JOB_ACTIONS, bulkJobActions, jobsFor } from '../../src/lib/logic/bulk.ts';

const job = (id: number, status: JobStatus) => ({ id, status });

describe('bulk job actions', () => {
	it('lists the four actions, in the order of the bar', () => {
		expect(BULK_JOB_ACTIONS).toEqual(['pause', 'resume', 'stop', 'retry-errors']);
	});

	it('each action applies to the same states as the job panel', () => {
		const all: JobStatus[] = ['queued', 'running', 'paused', 'stopping', 'stopped', 'done', 'done_with_errors', 'failed'];
		const list = all.map((status, i) => job(i + 1, status));
		const names = (action: Parameters<typeof jobsFor>[0]) => jobsFor(action, list).map((id) => all[id - 1]);
		expect(names('pause')).toEqual(['queued', 'running']);
		expect(names('resume')).toEqual(['paused']);
		expect(names('stop')).toEqual(['queued', 'running', 'paused']);
		expect(names('retry-errors')).toEqual(['stopped', 'done_with_errors', 'failed']);
	});

	it('a job that is stopping or finished cleanly is left alone by every action', () => {
		for (const status of ['stopping', 'done'] as const) {
			for (const action of BULK_JOB_ACTIONS) expect(jobsFor(action, [job(1, status)])).toEqual([]);
		}
	});

	it('mailboxes without a job are skipped, and the ids come back in the order given', () => {
		expect(jobsFor('pause', [undefined, job(7, 'running'), undefined, job(3, 'queued')])).toEqual([7, 3]);
		expect(jobsFor('pause', [])).toEqual([]);
		expect(jobsFor('stop', [undefined, undefined])).toEqual([]);
	});

	it('works on any iterable, a Set or a generator included', () => {
		const jobs = new Set([job(1, 'queued'), job(2, 'paused')]);
		expect(jobsFor('stop', jobs)).toEqual([1, 2]);
		function* gen() {
			yield job(4, 'failed');
			yield undefined;
		}
		expect(jobsFor('retry-errors', gen())).toEqual([4]);
		expect(bulkJobActions(gen())).toEqual(['retry-errors']);
	});

	it('the bar offers only what would change something', () => {
		expect(bulkJobActions([])).toEqual([]);
		expect(bulkJobActions([undefined, undefined])).toEqual([]);
		expect(bulkJobActions([job(1, 'done')])).toEqual([]);
		expect(bulkJobActions([job(1, 'queued')])).toEqual(['pause', 'stop']);
		expect(bulkJobActions([job(1, 'running')])).toEqual(['pause', 'stop']);
		expect(bulkJobActions([job(1, 'paused')])).toEqual(['resume', 'stop']);
		expect(bulkJobActions([job(1, 'failed')])).toEqual(['retry-errors']);
		expect(bulkJobActions([job(1, 'stopped')])).toEqual(['retry-errors']);
	});

	it('a mixed selection offers the union, never an action twice, always in the same order', () => {
		expect(bulkJobActions([job(1, 'paused'), job(2, 'running')])).toEqual(['pause', 'resume', 'stop']);
		expect(bulkJobActions([job(1, 'failed'), job(2, 'running'), job(3, 'done'), undefined])).toEqual(['pause', 'stop', 'retry-errors']);
		expect(bulkJobActions([job(1, 'running'), job(2, 'running')])).toEqual(['pause', 'stop']);
	});

	it('reads only the id and the status of a job', () => {
		const full = { id: 9, status: 'running', progress: {}, error: null } as unknown as Job;
		expect(jobsFor('pause', [full])).toEqual([9]);
	});
});

describe('import plan: refused connections', () => {
	const account = (over: Partial<Account> = {}): Account => ({
		id: 1,
		server_id: 1,
		email: 'alice@example.com',
		login: 'alice@example.com',
		has_password: true,
		status: 'ok',
		last_test: null,
		last_test_result: null,
		pst_id: 1,
		options: { mail: true, calendar: true, contacts: true },
		job: null,
		...over
	});
	const valid = { id: 1, status: 'valid' } as Pst;
	const plan = (accounts: Account[], jobs: Record<number, Job> = {}) => importPlan(accounts, (id) => jobs[id], () => valid);
	const ids = (list: Account[]) => list.map((a) => a.id);

	it('a refused login or an error at the last test is left out of "ready"', () => {
		const result = plan([account({ id: 1, status: 'auth_failed' }), account({ id: 2, status: 'error' }), account({ id: 3, status: 'ok' }), account({ id: 4, status: 'new' })]);
		expect(ids(result.refused)).toEqual([1, 2]);
		expect(ids(result.ready)).toEqual([3, 4]);
	});

	it('a mailbox never tested is not refused: only a failed test counts', () => {
		const result = plan([account({ status: 'new' })]);
		expect(result.refused).toEqual([]);
		expect(ids(result.ready)).toEqual([1]);
	});

	it('another problem comes first: no source, no password, or a job already running', () => {
		const result = plan(
			[
				account({ id: 1, status: 'auth_failed', pst_id: null }),
				account({ id: 2, status: 'auth_failed', has_password: false }),
				account({ id: 3, status: 'auth_failed' }),
				account({ id: 4, status: 'error' })
			],
			{ 3: { id: 30, status: 'running' } as Job }
		);
		expect(ids(result.noPst)).toEqual([1]);
		expect(ids(result.noPassword)).toEqual([2]);
		expect(ids(result.active)).toEqual([3]);
		expect(ids(result.refused)).toEqual([4]);
		expect(result.ready).toEqual([]);
	});

	it('every mailbox lands in exactly one list', () => {
		const list = [
			account({ id: 1 }),
			account({ id: 2, status: 'auth_failed' }),
			account({ id: 3, pst_id: null }),
			account({ id: 4, has_password: false }),
			account({ id: 5 })
		];
		const result = plan(list, { 5: { id: 50, status: 'queued' } as Job });
		const all = [...result.ready, ...result.noPst, ...result.noPassword, ...result.refused, ...result.active];
		expect(ids(all).sort()).toEqual([1, 2, 3, 4, 5]);
	});

	it('a source other than a file needs no PST, and a refused connection is still refused', () => {
		const imap = { kind: 'imap', config: {}, has_secret: true } as Account['source'];
		const result = plan([account({ id: 1, pst_id: null, source: imap, status: 'auth_failed' }), account({ id: 2, pst_id: null, source: imap })]);
		expect(ids(result.refused)).toEqual([1]);
		expect(ids(result.ready)).toEqual([2]);
		expect(result.noPst).toEqual([]);
	});

	it('an own destination with its secret stands in for the mailbox password', () => {
		const own = { kind: 'imap', config: {}, has_secret: true } as Account['target'];
		const result = plan([account({ id: 1, has_password: false, target: own, status: 'error' })]);
		expect(result.noPassword).toEqual([]);
		expect(ids(result.refused)).toEqual([1]);
	});

	it('nothing selected gives empty lists', () => {
		expect(plan([])).toEqual({ ready: [], noPst: [], noPassword: [], refused: [], active: [], noDav: [] });
	});
});
