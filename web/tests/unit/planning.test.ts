import { describe, expect, it } from 'vitest';
import type { Account, Job, Plan, PlanAccount, Pst } from '../../src/lib/api/types.ts';
import { matchesFilter } from '../../src/lib/logic/accounts.ts';
import { batchActions, batchItems, batchLookStatus } from '../../src/lib/logic/batch.ts';
import { hasCustomSource, isFileKind, kindsFor } from '../../src/lib/logic/connector.ts';
import { foldersToCreate, planDone, planProblem, planRows } from '../../src/lib/logic/plan.ts';
import { effectiveLines, emptyPolicyForm, policyBody, policyChanged, policyToForm } from '../../src/lib/logic/policy.ts';
import { canPreview, FILE_ACCEPT, formatOf } from '../../src/lib/logic/pst.ts';
import { moveInQueue, prioritiesOf, queueOrder } from '../../src/lib/logic/queue.ts';

const job = (id: number, status: Job['status'], priority = 0): Pick<Job, 'id' | 'status' | 'priority'> => ({ id, status, priority });

describe('queue order', () => {
	it('follows priority, then age, and keeps only queued jobs', () => {
		const jobs = [job(1, 'queued'), job(2, 'running'), job(3, 'queued', 5), job(4, 'queued')];
		expect(queueOrder(jobs)).toEqual([3, 1, 4]);
	});

	it('moves a job one place, and refuses at the ends', () => {
		expect(moveInQueue([3, 1, 4], 1, -1)).toEqual([1, 3, 4]);
		expect(moveInQueue([3, 1, 4], 1, 1)).toEqual([3, 4, 1]);
		expect(moveInQueue([3, 1, 4], 3, -1)).toBeNull();
		expect(moveInQueue([3, 1, 4], 4, 1)).toBeNull();
		expect(moveInQueue([3, 1, 4], 9, 1)).toBeNull();
	});

	it('numbers the new order from the end, as the server does', () => {
		const p = prioritiesOf([7, 8, 9]);
		expect(p.get(7)).toBe(3);
		expect(p.get(9)).toBe(1);
		// The numbers give back the same order.
		expect(queueOrder([job(7, 'queued', 3), job(8, 'queued', 2), job(9, 'queued', 1)])).toEqual([7, 8, 9]);
	});
});

describe('batches', () => {
	const by = (o: Record<string, number>) => ({ by_status: o, failed: o.failed ?? 0, with_errors: o.done_with_errors ?? 0 });

	it('offers only the actions that change something', () => {
		expect(batchActions(by({ running: 2, queued: 1 }))).toEqual(['pause', 'stop']);
		expect(batchActions(by({ paused: 1 }))).toEqual(['resume', 'stop']);
		expect(batchActions(by({ done: 3 }))).toEqual([]);
		expect(batchActions(by({ done: 1, failed: 2 }))).toEqual(['retry-failed']);
		expect(batchActions(by({ done_with_errors: 1 }))).toEqual(['retry-failed']);
	});

	it('says a finished batch with problems is not simply done', () => {
		expect(batchLookStatus({ state: 'finished', failed: 0, with_errors: 0 })).toBe('done');
		expect(batchLookStatus({ state: 'finished', failed: 0, with_errors: 2 })).toBe('done_with_errors');
		expect(batchLookStatus({ state: 'finished', failed: 1, with_errors: 2 })).toBe('failed');
		expect(batchLookStatus({ state: 'empty', failed: 0, with_errors: 0 })).toBe('done');
		expect(batchLookStatus({ state: 'running', failed: 0, with_errors: 0 })).toBe('running');
	});

	it('sums the items of every kind', () => {
		const p = (done: number, total: number, errors = 0) => ({ done, total, errors });
		expect(batchItems({ items: { mail: p(5, 10, 1), calendar: p(1, 2), contacts: p(0, 0) } })).toEqual({ done: 6, total: 12, errors: 1 });
	});

	it('filters the accounts to the jobs of one batch', () => {
		const account = { id: 1, email: 'a@x.org', login: null, server_id: 1, pst_id: 1 } as Account;
		const filter = { q: '', server: null, status: '' as const, batch: 4 };
		expect(matchesFilter(account, { batch_id: 4 } as Job, filter)).toBe(true);
		expect(matchesFilter(account, { batch_id: 5 } as Job, filter)).toBe(false);
		expect(matchesFilter(account, undefined, filter)).toBe(false);
		expect(matchesFilter(account, undefined, { ...filter, batch: null })).toBe(true);
	});
});

describe('dry run plan', () => {
	const kind = (over: Record<string, unknown> = {}) => ({ folders: 1, items: 10, bytes: 1000, done: 0, to_migrate: 8, to_migrate_bytes: 800, filtered: { date: 1, size: 1 }, oversized: 0, oversized_bytes: 0, folders_to_create: 1, excluded_folders: 0, excluded_items: 0, ...over });

	it('is over when done or failed', () => {
		expect(planDone({ status: 'queued' } as Plan)).toBe(false);
		expect(planDone({ status: 'running' } as Plan)).toBe(false);
		expect(planDone({ status: 'done' } as Plan)).toBe(true);
		expect(planDone({ status: 'failed' } as Plan)).toBe(true);
	});

	it('lists a row per kind found, with the filtered reasons added up', () => {
		const rows = planRows({ kinds: { mail: kind(), contacts: kind({ to_migrate_bytes: null }) } });
		expect(rows.map((r) => r.kind)).toEqual(['mail', 'contacts']);
		expect(rows[0]).toMatchObject({ toMigrate: 8, bytes: 800, filtered: 2, oversized: 0 });
		expect(rows[1].bytes).toBeNull();
		expect(planRows({})).toEqual([]);
	});

	it('names the folders to create once each, and counts what was cut', () => {
		const f = (target: string, create: boolean | null, excluded = false) => ({ path: ['x'], items: 1, bytes: 1, to_migrate: 1, target, create, excluded });
		const names = foldersToCreate({ folders: [f('A', true), f('A', true), f('B', false), f('C', null), f('D', true, true), f('E', true)] });
		expect(names).toEqual({ names: ['A', 'E'], rest: 0 });
		const many = foldersToCreate({ folders: Array.from({ length: 15 }, (_, i) => f(`F${i}`, true)) });
		expect(many.names).toHaveLength(12);
		expect(many.rest).toBe(3);
		expect(foldersToCreate({})).toEqual({ names: [], rest: 0 });
	});

	it('gives the reason of an account that could not be read', () => {
		expect(planProblem({ account_id: 1, status: 'error', source: { kind: 'imap', ok: false, msg: 'LOGIN failed' } } as PlanAccount)).toBe('LOGIN failed');
		expect(planProblem({ account_id: 1, status: 'error', error: 'boom' } as PlanAccount)).toBe('boom');
		expect(planProblem({ account_id: 1, status: 'ok' } as PlanAccount)).toBe('');
	});
});

describe('policy form', () => {
	it('round-trips what the server stored', () => {
		const stored = { folder_map: [{ source: 'Old', target: '@sent' }], exclude_folders: ['Junk*', 'Spam'], date_from: '2020-01-01', max_size_mb: 25, window: { days: [0, 4], start: '20:00', end: '06:00', tz: 'Europe/Paris' }, rate_mb_s: 5 };
		const form = policyToForm(stored);
		expect(form.exclude).toBe('Junk*\nSpam');
		expect(policyBody(form, true)).toEqual(stored);
	});

	it('leaves empty fields out, and an empty form clears the policy', () => {
		expect(policyBody(emptyPolicyForm(), true)).toEqual({});
		const form = { ...emptyPolicyForm(), map: [{ source: '', target: '' }], exclude: ' \n ' };
		expect(policyBody(form, false)).toEqual({});
	});

	it('writes hours and bandwidth for a server only', () => {
		const form = { ...emptyPolicyForm(), start: '22:00', rate: '2,5', maxSize: '10' };
		expect(policyBody(form, false)).toEqual({ max_size_mb: 10 });
		expect(policyBody(form, true)).toEqual({ max_size_mb: 10, window: { start: '22:00', end: '00:00', tz: 'UTC' }, rate_mb_s: 2.5 });
	});

	it('sends what is not a number as typed, so that the server explains', () => {
		expect(policyBody({ ...emptyPolicyForm(), maxSize: 'big' }, false)).toEqual({ max_size_mb: 'big' });
	});

	it('notices a change', () => {
		const a = policyToForm({ max_size_mb: 1 });
		expect(policyChanged(a, policyToForm({ max_size_mb: 1 }))).toBe(false);
		expect(policyChanged(a, { ...a, dateTo: '2021-01-01' })).toBe(true);
	});

	it('lists the effective values in a fixed order', () => {
		const lines = effectiveLines({ max_size_mb: 25, folder_map: [{ source: 'A', target: 'B' }], window: { days: [0], start: '20:00', end: '06:00', tz: 'UTC' } }, (k) => k, (d) => `d${d}`);
		expect(lines.map((l) => l.key)).toEqual(['map', 'maxSize', 'window']);
		expect(lines[0].value).toBe('A > B');
		expect(lines[2].value).toBe('d0 20:00-06:00 (UTC)');
		expect(effectiveLines(undefined, (k) => k, String)).toEqual([]);
	});
});

describe('files of every kind', () => {
	it('accepts every format the server analyses', () => {
		for (const ext of ['pst', 'ost', 'mbox', 'mbx', 'eml', 'msg', 'ics', 'vcf', 'zip']) expect(FILE_ACCEPT.split(',')).toContain(`.${ext}`);
	});

	it('names the format of a scan', () => {
		expect(formatOf(null)).toBe('');
		expect(formatOf({})).toBe('');
		expect(formatOf({ format: 'files', contents: ['mbox', 'eml'] })).toBe('mbox, eml');
		expect(formatOf({ format: 'files' })).toBe('files');
	});

	it('previews a valid PST only', () => {
		expect(canPreview({ status: 'valid', scan: { folders: [] } } as unknown as Pst)).toBe(true);
		expect(canPreview({ status: 'valid', scan: { format: 'files' } } as unknown as Pst)).toBe(false);
		expect(canPreview({ status: 'invalid', scan: null } as unknown as Pst)).toBe(false);
	});

	it('reads the files source like the PST: from the Files page, not from the route form', () => {
		expect(isFileKind('files')).toBe(true);
		expect(isFileKind('pst')).toBe(true);
		expect(isFileKind('imap')).toBe(false);
		expect(hasCustomSource({ source: { kind: 'files', config: { pst_id: 3 }, has_secret: false } })).toBe(false);
		const role = { items: [], capabilities: [], fields: [] };
		const catalogue = [
			{ kind: 'files', label: 'Files', description: '', source: role, target: null },
			{ kind: 'imap', label: 'IMAP', description: '', source: role, target: role }
		];
		expect(kindsFor(catalogue, 'source').map((c) => c.kind)).toEqual(['imap']);
	});
});
