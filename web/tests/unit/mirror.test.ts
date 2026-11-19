import { describe, expect, it } from 'vitest';
import type { MirrorAccountPlan, MirrorSample } from '../../src/lib/api/types.ts';
import {
	accountTotals,
	BLOCK_CODES,
	blockCodes,
	blockedBy,
	blockInfo,
	canApply,
	canMirrorResync,
	clampCount,
	clampPercent,
	DEFAULT_MAX_DELETE_PERCENT,
	DEFAULT_MIN_DELETE_COUNT,
	describeNote,
	type EmptyFolder,
	emptyFolderCount,
	emptyFolderOf,
	hasWarningNote,
	MAX_MIN_DELETE_COUNT,
	mergeEmptyFolders,
	mirrorReport,
	mirrorReportRows,
	mirrorRows,
	mirrorSkipped,
	mirrorState,
	mirrorTotals,
	neededPercent,
	NOTE_CODES,
	type NoteLabels,
	noteLines,
	noteTone,
	offersLargeDelete,
	readyIds,
	sampleLines,
	startedJobs,
	sumCounts,
	toggleEmptyFolder
} from '../../src/lib/logic/mirror.ts';

const plan = (id: number, over: Partial<MirrorAccountPlan> = {}): MirrorAccountPlan => ({
	account_id: id,
	email: `u${id}@example.com`,
	ready: true,
	to_add: { mail: 0, calendar: 0, contacts: 0, tasks: 0 },
	to_update: { mail: 0, calendar: 0, contacts: 0, tasks: 0 },
	to_delete: { mail: 0, calendar: 0, contacts: 0, tasks: 0 },
	blocked: [],
	sample: [],
	...over
});
const blocked = (id: number, ...codes: string[]) => plan(id, { ready: false, blocked: codes.map((code) => ({ code, message: `msg ${code}` })), to_delete: { mail: 50 } });

describe('mirror counts and totals', () => {
	it('adds every kind, counts nothing for a missing or negative number', () => {
		expect(sumCounts({ mail: 3, calendar: 2, contacts: 1, tasks: 4 })).toBe(10);
		expect(sumCounts({ mail: 3 })).toBe(3);
		expect(sumCounts({ mail: -2, tasks: 1 })).toBe(1);
		expect(sumCounts(undefined)).toBe(0);
	});

	it('totals one mailbox by action', () => {
		const a = plan(1, { to_add: { mail: 10, tasks: 1 }, to_update: { calendar: 2 }, to_delete: { mail: 5, contacts: 2 } });
		expect(accountTotals(a)).toEqual({ add: 11, update: 2, delete: 7 });
	});

	it('the total of a run only counts the mailboxes that can run', () => {
		const accounts = [plan(1, { to_delete: { mail: 4 }, to_add: { mail: 1 } }), blocked(2, 'too_many_deletes'), plan(3, { to_update: { mail: 2 }, to_delete: { contacts: 1 } })];
		expect(mirrorTotals(accounts)).toEqual({ add: 1, update: 2, delete: 5 });
		expect(readyIds(accounts)).toEqual([1, 3]);
	});

	it('lists only the kinds with something to do, in the fixed order', () => {
		const rows = mirrorRows(plan(1, { to_delete: { tasks: 1 }, to_add: { mail: 2 }, to_update: { contacts: 3 } }));
		expect(rows.map((r) => r.kind)).toEqual(['mail', 'contacts', 'tasks']);
		expect(rows[0]).toEqual({ kind: 'mail', add: 2, update: 0, delete: 0 });
		expect(mirrorRows(plan(1))).toEqual([]);
	});
});

describe('state of the whole preview', () => {
	it('is empty before an answer', () => {
		expect(mirrorState([])).toBe('empty');
		expect(canApply('empty')).toBe(false);
	});

	it('is blocked when no mailbox can run, even if it would delete', () => {
		expect(mirrorState([blocked(1, 'mirror_off'), blocked(2, 'no_prior_run')])).toBe('blocked');
		expect(canApply('blocked')).toBe(false);
	});

	it('is up to date when a mailbox is ready and nothing differs', () => {
		expect(mirrorState([plan(1), blocked(2, 'source_empty')])).toBe('uptodate');
		expect(canApply('uptodate')).toBe(false);
	});

	it('is additive without deletions and destructive with one, blocked mailboxes not counted', () => {
		expect(mirrorState([plan(1, { to_add: { mail: 3 } })])).toBe('additive');
		expect(mirrorState([plan(1, { to_update: { mail: 1 } }), blocked(2, 'too_many_deletes')])).toBe('additive');
		expect(mirrorState([plan(1, { to_delete: { calendar: 1 } })])).toBe('destructive');
		expect(canApply('additive') && canApply('destructive')).toBe(true);
	});
});

describe('blocks', () => {
	it('knows the seven codes of the API and offers a fix for three', () => {
		expect([...BLOCK_CODES].sort()).toEqual(['job_active', 'mirror_off', 'no_prior_run', 'source_empty', 'source_unlistable', 'target_unreachable', 'too_many_deletes', 'uidvalidity_changed', 'unknown_account']);
		for (const code of BLOCK_CODES) expect(blockInfo(code).known).toBe(true);
		expect(blockInfo('mirror_off').fix).toBe('enable');
		expect(blockInfo('too_many_deletes').fix).toBe('threshold');
		expect(blockInfo('source_empty').fix).toBe('allow_empty');
		expect(blockInfo('no_prior_run').fix).toBeNull();
		expect(blockInfo('something_new')).toEqual({ known: false, fix: null });
	});

	it('lists each code once, in the order of the list, unknown ones last', () => {
		const accounts = [blocked(1, 'target_unreachable', 'brand_new'), blocked(2, 'mirror_off', 'target_unreachable'), plan(3)];
		expect(blockCodes(accounts)).toEqual(['mirror_off', 'target_unreachable', 'brand_new']);
		expect(blockedBy(accounts, 'target_unreachable').map((a) => a.account_id)).toEqual([1, 2]);
		expect(blockedBy(accounts, 'source_empty')).toEqual([]);
	});

	it('offers the large deletion only for a refused deletion', () => {
		expect(offersLargeDelete([blocked(1, 'too_many_deletes')])).toBe(true);
		expect(offersLargeDelete([blocked(1, 'source_empty'), plan(2)])).toBe(false);
		expect(offersLargeDelete([])).toBe(false);
	});
});

describe('empty folders', () => {
	const empty = (id: number, folder: string, over: Record<string, unknown> = {}) =>
		plan(id, { ready: false, blocked: [{ code: 'source_empty', kind: 'mail', folder, message: `mail folder '${folder}': the source lists nothing while 42 item(s) were migrated; if it was emptied on purpose, run again with allow_empty_folders: ["${folder}"]`, ...over }] });

	it('reads the folder, the kind and the number of items out of a source_empty block', () => {
		expect(emptyFolderOf(empty(1, 'Projects').blocked[0])).toEqual({ key: 'Projects', kind: 'mail', count: 42 });
		expect(emptyFolderOf(empty(1, 'Cal', { kind: 'calendar', message: 'no number here' }).blocked[0])).toEqual({ key: 'Cal', kind: 'calendar', count: null });
		expect(emptyFolderOf(empty(1, 'X', { kind: 'bogus' }).blocked[0])?.kind).toBeNull();
	});

	it('a block that names no folder, or another code, cannot be accepted one by one', () => {
		expect(emptyFolderOf({ code: 'source_empty', message: 'old server' })).toBeNull();
		expect(emptyFolderOf({ code: 'source_empty', folder: '', message: '' })).toBeNull();
		expect(emptyFolderOf({ code: 'too_many_deletes', folder: 'Projects', message: '' })).toBeNull();
	});

	it('takes the number of items from the message only when it says one', () => {
		expect(emptyFolderCount('the source lists nothing while 7 item(s) were migrated')).toBe(7);
		expect(emptyFolderCount('a newer wording')).toBeNull();
		expect(emptyFolderCount(undefined)).toBeNull();
	});

	it('lists each blocked folder once, and keeps the accepted ones after their block is gone', () => {
		const first = mergeEmptyFolders(new Map(), [empty(1, 'Projects'), empty(2, 'Projects'), empty(2, 'Archive'), plan(3)], new Set());
		expect([...first.keys()]).toEqual(['Projects', 'Archive']);
		// Projects is accepted: the next answer no longer blocks it, but it stays; Archive was neither blocked nor accepted any more.
		const next = mergeEmptyFolders(first, [plan(1), plan(2)], new Set(['Projects']));
		expect([...next.keys()]).toEqual(['Projects']);
		expect(next.get('Projects')).toEqual<EmptyFolder>({ key: 'Projects', kind: 'mail', count: 42 });
		// A key accepted that was never seen shows nothing.
		expect(mergeEmptyFolders(new Map(), [], new Set(['Ghost'])).size).toBe(0);
	});

	it('accepting or taking back a folder gives a new set and leaves the other ones alone', () => {
		const none = new Set<string>();
		const one = toggleEmptyFolder(none, 'A', true);
		expect([...none]).toEqual([]);
		expect([...toggleEmptyFolder(one, 'B', true)]).toEqual(['A', 'B']);
		expect([...toggleEmptyFolder(toggleEmptyFolder(one, 'B', true), 'A', false)]).toEqual(['B']);
		expect([...toggleEmptyFolder(one, 'A', true)]).toEqual(['A']);
	});
});

describe('threshold of a refused deletion', () => {
	it('gives the percentage that lets the deletions through, as a whole number from 1 to 100', () => {
		expect(neededPercent({ code: 'too_many_deletes', needed_percent: 30 })).toBe(30);
		expect(neededPercent({ code: 'too_many_deletes', needed_percent: 30.2 })).toBe(31);
		expect(neededPercent({ code: 'too_many_deletes', needed_percent: 250 })).toBe(100);
		expect(neededPercent({ code: 'too_many_deletes', needed_percent: 0 })).toBe(1);
	});

	it('says nothing when the block does not carry it, or is another block', () => {
		expect(neededPercent({ code: 'too_many_deletes' })).toBeNull();
		expect(neededPercent({ code: 'too_many_deletes', needed_percent: Number.NaN })).toBeNull();
		expect(neededPercent({ code: 'source_empty', needed_percent: 30 })).toBeNull();
	});
});

describe('minimal number of deletions', () => {
	it('stays a whole number from 0', () => {
		expect(clampCount('25')).toBe(25);
		expect(clampCount('0')).toBe(0);
		expect(clampCount('-4')).toBe(0);
		expect(clampCount(7.6)).toBe(8);
		expect(clampCount('3,2')).toBe(3);
		expect(clampCount('999999999999')).toBe(MAX_MIN_DELETE_COUNT);
	});

	it('goes back to the default when it is not a number', () => {
		expect(clampCount('')).toBe(DEFAULT_MIN_DELETE_COUNT);
		expect(clampCount('abc')).toBe(DEFAULT_MIN_DELETE_COUNT);
		expect(DEFAULT_MIN_DELETE_COUNT).toBe(10);
	});
});

describe('jobs of a run', () => {
	it('leaves out a job that was given back as it is', () => {
		const job = (id: number, over: Record<string, unknown> = {}) => ({ id, account_id: id, status: 'queued', ...over }) as never;
		expect(startedJobs([job(1), job(2, { existing: true }), job(3, { existing: false })]).map((j) => j.id)).toEqual([1, 3]);
	});
});

describe('safety limit', () => {
	it('stays a whole number from 1 to 100', () => {
		expect(clampPercent('35')).toBe(35);
		expect(clampPercent(12.6)).toBe(13);
		expect(clampPercent('0')).toBe(1);
		expect(clampPercent('250')).toBe(100);
		expect(clampPercent('7,4')).toBe(7);
	});

	it('goes back to the default when it is not a number', () => {
		expect(clampPercent('')).toBe(DEFAULT_MAX_DELETE_PERCENT);
		expect(clampPercent('abc')).toBe(DEFAULT_MAX_DELETE_PERCENT);
		expect(DEFAULT_MAX_DELETE_PERCENT).toBe(20);
	});
});

describe('sample', () => {
	const line = (action: MirrorSample['action'], n: number): MirrorSample => ({ kind: 'mail', action, folder: 'Inbox', subject: `${action} ${n}`, date: '2026-03-01T10:00:00Z' });

	it('shows deletions first, then updates, then additions, and keeps 20 lines', () => {
		const sample = [line('add', 1), line('delete', 1), line('update', 1), line('add', 2), ...Array.from({ length: 30 }, (_, i) => line('add', i + 10))];
		const lines = sampleLines({ sample });
		expect(lines).toHaveLength(20);
		expect(lines.slice(0, 3).map((l) => l.subject)).toEqual(['delete 1', 'update 1', 'add 1']);
	});

	it('is empty without a sample', () => {
		expect(sampleLines({ sample: [] })).toEqual([]);
		expect(sampleLines({} as MirrorAccountPlan)).toEqual([]);
	});
});

describe('job report', () => {
	it('has a mirror section only when the report carries one', () => {
		expect(mirrorReport({ mail: {} })).toBeNull();
		expect(mirrorReport(null)).toBeNull();
		expect(mirrorReport({ mirror: [] })).toBeNull();
		const real = { options: { max_delete_percent: 20, allow_large_delete: false }, blocked: [], mail: { added: 1, updated: 0, deleted: 0, skipped_by_safety: 0, errors: 0 } };
		expect(mirrorReport({ mirror: real })).toEqual(real);
	});

	it('gives one line per kind that moved, was left out or failed', () => {
		const c = (added: number, updated: number, deleted: number, skipped_by_safety: number, errors: number) => ({ added, updated, deleted, skipped_by_safety, errors });
		// The shape of the server (connector_runner._run_mirror): the counters are keyed by kind.
		const report = { options: { max_delete_percent: 20, allow_large_delete: false }, blocked: [], mail: c(4, 1, 3, 0, 0), calendar: c(0, 0, 0, 6, 0), contacts: c(0, 2, 0, 0, 0), tasks: c(0, 0, 0, 0, 1) };
		const rows = mirrorReportRows(report);
		expect(rows.map((r) => r.kind)).toEqual(['mail', 'calendar', 'contacts', 'tasks']);
		expect(rows[0]).toEqual({ kind: 'mail', added: 4, updated: 1, deleted: 3, skipped: 0, errors: 0 });
		expect(rows[1].skipped).toBe(6);
		expect(mirrorSkipped(report)).toBe(6);
		expect(mirrorReportRows({ mail: c(0, 0, 0, 0, 0) })).toEqual([]);
		expect(mirrorReportRows({ blocked: [{ code: 'source_empty', message: 'x' }] })).toEqual([]);
	});
});

describe('who can be resynced', () => {
	it('a mailbox without a job or with a finished one, never one that is queued, running or paused', () => {
		expect(canMirrorResync(undefined)).toBe(true);
		for (const status of ['done', 'done_with_errors', 'stopped', 'failed'] as const) expect(canMirrorResync({ status })).toBe(true);
		for (const status of ['queued', 'running', 'paused', 'stopping'] as const) expect(canMirrorResync({ status })).toBe(false);
	});
});

describe('notes', () => {
	const labels: NoteLabels = {
		soft_delete: { label: 'Soft', hint: 'soft hint' },
		hard_delete: { label: 'Hard', hint: 'hard hint' },
		no_copy_kept: { label: 'No copy', hint: 'no copy hint' },
		shared_resource: { label: 'Shared', hint: 'shared hint' },
		target_modified: { label: 'Modified', hint: 'modified hint' }
	};

	it('knows the five codes; a soft or a permanent deletion is a warning, the others plain notes', () => {
		expect([...NOTE_CODES].sort()).toEqual(['hard_delete', 'no_copy_kept', 'shared_resource', 'soft_delete', 'target_modified']);
		expect(noteTone('soft_delete')).toBe('warning');
		expect(noteTone('hard_delete')).toBe('warning');
		expect(noteTone('no_copy_kept')).toBe('note');
		expect(noteTone('shared_resource')).toBe('note');
		expect(noteTone('target_modified')).toBe('note');
		expect(noteTone('brand_new')).toBe('note');
	});

	it('a known code gets the label of the interface and keeps the API message as its detail', () => {
		expect(describeNote({ code: 'soft_delete', message: 'no Trash on the destination' }, labels)).toEqual({ code: 'soft_delete', tone: 'warning', label: 'Soft', hint: 'soft hint', detail: 'no Trash on the destination' });
		expect(describeNote({ code: 'shared_resource', message: '  ' }, labels).detail).toBeNull();
	});

	it('target_modified is a plain note with its label and hint, and does not open a mailbox', () => {
		expect(describeNote({ code: 'target_modified', message: '2 contacts changed at the destination' }, labels)).toEqual({ code: 'target_modified', tone: 'note', label: 'Modified', hint: 'modified hint', detail: '2 contacts changed at the destination' });
		expect(hasWarningNote([{ code: 'target_modified', message: '' }])).toBe(false);
	});

	it('an unknown code shows the API message as its label, or the code when it has none', () => {
		expect(describeNote({ code: 'brand_new', message: 'Something new happens' }, labels)).toEqual({ code: 'brand_new', tone: 'note', label: 'Something new happens', hint: null, detail: null });
		expect(describeNote({ code: 'brand_new', message: '' }, labels).label).toBe('brand_new');
	});

	it('lists warnings first, keeps the order otherwise, and drops a note repeated as is', () => {
		const notes = [
			{ code: 'shared_resource', message: 'a' },
			{ code: 'hard_delete', message: 'b' },
			{ code: 'brand_new', message: 'c' },
			{ code: 'shared_resource', message: 'a' },
			{ code: 'soft_delete', message: 'd' },
			{ code: 'shared_resource', message: 'e' }
		];
		expect(noteLines(notes, labels).map((n) => `${n.code}:${n.detail ?? n.label}`)).toEqual(['hard_delete:b', 'soft_delete:d', 'shared_resource:a', 'brand_new:c', 'shared_resource:e']);
		expect(noteLines(undefined, labels)).toEqual([]);
		expect(noteLines(null, labels)).toEqual([]);
	});

	it('only a soft or a permanent deletion makes a mailbox open on its notes', () => {
		expect(hasWarningNote(undefined)).toBe(false);
		expect(hasWarningNote([{ code: 'no_copy_kept', message: '' }, { code: 'shared_resource', message: '' }])).toBe(false);
		expect(hasWarningNote([{ code: 'shared_resource', message: '' }, { code: 'hard_delete', message: '' }])).toBe(true);
	});

	it('the notes of a job report are read where the server puts them', () => {
		const report = { mirror: { notes: [{ code: 'soft_delete', message: 'm' }] } };
		expect(noteLines(mirrorReport(report)?.notes, labels).map((n) => n.code)).toEqual(['soft_delete']);
		expect(noteLines(mirrorReport({ mirror: { blocked: [] } })?.notes, labels)).toEqual([]);
	});
});
