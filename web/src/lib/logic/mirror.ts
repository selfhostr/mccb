import type { Job, MirrorAccountPlan, MirrorBlock, MirrorCounts, MirrorKind, MirrorNote, MirrorReport, MirrorSample } from '../api/types.ts';

/** Mirror mode: a rerun makes the destination identical to the source. The source is never written. */
export const MIRROR_KINDS: MirrorKind[] = ['mail', 'calendar', 'contacts', 'tasks'];
/** The lines of the preview that are shown, per mailbox. */
export const MIRROR_SAMPLE_LIMIT = 20;
/** The safety threshold of the server: the most a run may delete, in percent of what the destination holds. */
export const DEFAULT_MAX_DELETE_PERCENT = 20;
/** Below this many deletions of a kind, the percentage of the threshold does not apply (the default of the server). */
export const DEFAULT_MIN_DELETE_COUNT = 10;
/** The most the field accepts: far above anything a mailbox holds, and a safe integer. */
export const MAX_MIN_DELETE_COUNT = 1_000_000;

/** Every count of one object, all kinds together. Absent or negative counts weigh nothing. */
export function sumCounts(counts: MirrorCounts | undefined): number {
	let total = 0;
	for (const kind of MIRROR_KINDS) {
		const n = counts?.[kind];
		if (typeof n === 'number' && n > 0) total += n;
	}
	return total;
}

export interface MirrorTotals {
	add: number;
	update: number;
	delete: number;
}

export function accountTotals(account: Pick<MirrorAccountPlan, 'to_add' | 'to_update' | 'to_delete'>): MirrorTotals {
	return { add: sumCounts(account.to_add), update: sumCounts(account.to_update), delete: sumCounts(account.to_delete) };
}

/** What applying would do: the mailboxes that are ready, the only ones that run. */
export function mirrorTotals(accounts: readonly MirrorAccountPlan[]): MirrorTotals {
	const total: MirrorTotals = { add: 0, update: 0, delete: 0 };
	for (const account of accounts) {
		if (!account.ready) continue;
		const own = accountTotals(account);
		total.add += own.add;
		total.update += own.update;
		total.delete += own.delete;
	}
	return total;
}

export const readyAccounts = (accounts: readonly MirrorAccountPlan[]): MirrorAccountPlan[] => accounts.filter((a) => a.ready);
export const readyIds = (accounts: readonly MirrorAccountPlan[]): number[] => readyAccounts(accounts).map((a) => a.account_id);

/**
 * The state of the whole preview, which decides the apply button:
 * `empty` no answer yet, `blocked` no mailbox may run, `uptodate` the destinations already match,
 * `additive` only additions and updates, `destructive` something would be deleted.
 */
export type MirrorState = 'empty' | 'blocked' | 'uptodate' | 'additive' | 'destructive';

export function mirrorState(accounts: readonly MirrorAccountPlan[]): MirrorState {
	if (!accounts.length) return 'empty';
	if (!readyAccounts(accounts).length) return 'blocked';
	const total = mirrorTotals(accounts);
	if (total.delete > 0) return 'destructive';
	return total.add + total.update > 0 ? 'additive' : 'uptodate';
}

/** Only these states can be applied. */
export const canApply = (state: MirrorState): boolean => state === 'additive' || state === 'destructive';

export interface MirrorRow {
	kind: MirrorKind;
	add: number;
	update: number;
	delete: number;
}

/** One line per kind that has something to do on this mailbox. */
export function mirrorRows(account: Pick<MirrorAccountPlan, 'to_add' | 'to_update' | 'to_delete'>): MirrorRow[] {
	const rows: MirrorRow[] = [];
	for (const kind of MIRROR_KINDS) {
		const row = { kind, add: account.to_add?.[kind] ?? 0, update: account.to_update?.[kind] ?? 0, delete: account.to_delete?.[kind] ?? 0 };
		if (row.add > 0 || row.update > 0 || row.delete > 0) rows.push(row);
	}
	return rows;
}

/** The first lines of the sample, deletions first: they are what the reader has to check. */
export function sampleLines(account: Pick<MirrorAccountPlan, 'sample'>, limit = MIRROR_SAMPLE_LIMIT): MirrorSample[] {
	const order = { delete: 0, update: 1, add: 2 } as const;
	return (account.sample ?? [])
		.map((line, index) => ({ line, index }))
		.sort((a, b) => (order[a.line.action] ?? 3) - (order[b.line.action] ?? 3) || a.index - b.index)
		.slice(0, limit)
		.map((item) => item.line);
}

/** The reasons that stop a mirror run. What to do about each one is said in the interface; the message comes from the API. */
export const BLOCK_CODES = ['mirror_off', 'source_unlistable', 'too_many_deletes', 'source_empty', 'no_prior_run', 'uidvalidity_changed', 'target_unreachable', 'job_active', 'unknown_account'] as const;
export type BlockCode = (typeof BLOCK_CODES)[number];

/**
 * `fix` is the action the dialog can offer on its own: `enable` turns the mirror on for the mailbox,
 * `threshold` opens the safety limit, `allow_empty` accepts an empty folder (when the block names it, see `emptyFolderOf`).
 * The others need something done elsewhere and are only told.
 */
export type BlockFix = 'enable' | 'threshold' | 'allow_empty' | null;

export interface BlockInfo {
	/** A code of the list above; false for one this version does not know (its API message is shown as is). */
	known: boolean;
	fix: BlockFix;
}

export function blockInfo(code: string): BlockInfo {
	const known = (BLOCK_CODES as readonly string[]).includes(code);
	const fix: BlockFix = code === 'mirror_off' ? 'enable' : code === 'too_many_deletes' ? 'threshold' : code === 'source_empty' ? 'allow_empty' : null;
	return { known, fix };
}

/** The distinct blocking codes of the preview, in the order of the list above, unknown ones last. */
export function blockCodes(accounts: readonly MirrorAccountPlan[]): string[] {
	const seen = new Set<string>();
	for (const account of accounts) for (const block of account.blocked ?? []) seen.add(block.code);
	const rank = (code: string) => {
		const i = (BLOCK_CODES as readonly string[]).indexOf(code);
		return i < 0 ? BLOCK_CODES.length : i;
	};
	return [...seen].sort((a, b) => rank(a) - rank(b));
}

/** The mailboxes stopped by one code. */
export const blockedBy = (accounts: readonly MirrorAccountPlan[], code: string): MirrorAccountPlan[] => accounts.filter((a) => a.blocked?.some((b) => b.code === code));

/** Only a `too_many_deletes` block can be lifted from the dialog, so only then is the large-deletion box offered. */
export const offersLargeDelete = (accounts: readonly MirrorAccountPlan[]): boolean => accounts.some((a) => a.blocked?.some((b) => b.code === 'too_many_deletes'));

/** A folder that is empty at the source while the destination holds what was migrated from it: its deletions are held until it is accepted. */
export interface EmptyFolder {
	/** The key to pass in `allow_empty_folders`. */
	key: string;
	kind: MirrorKind | null;
	/** How many items the destination would lose; null when the message of the API does not say. */
	count: number | null;
}

/** The server words it "... the source lists nothing while 42 item(s) were migrated ..."; a newer wording gives no number, and the sentence goes without. */
export function emptyFolderCount(message: string | null | undefined): number | null {
	const found = /while (\d+) item/.exec(message ?? '');
	return found ? Number(found[1]) : null;
}

/** The folder a `source_empty` block names, or null for any other block (or one that names no folder: it cannot be accepted one by one). */
export function emptyFolderOf(block: Pick<MirrorBlock, 'code' | 'folder' | 'kind' | 'message'>): EmptyFolder | null {
	if (block.code !== 'source_empty' || typeof block.folder !== 'string' || !block.folder) return null;
	const kind = (MIRROR_KINDS as readonly string[]).includes(block.kind ?? '') ? (block.kind as MirrorKind) : null;
	return { key: block.folder, kind, count: emptyFolderCount(block.message) };
}

/**
 * The empty folders to show after a comparison: those the answer blocks, and those already accepted (their block is gone
 * since they were, but they stay listed so that the choice can be taken back). A folder neither blocked nor accepted is forgotten.
 */
export function mergeEmptyFolders(previous: ReadonlyMap<string, EmptyFolder>, accounts: readonly MirrorAccountPlan[], allowed: ReadonlySet<string>): Map<string, EmptyFolder> {
	const next = new Map<string, EmptyFolder>();
	for (const account of accounts) {
		for (const block of account.blocked ?? []) {
			const folder = emptyFolderOf(block);
			if (folder && !next.has(folder.key)) next.set(folder.key, folder);
		}
	}
	for (const key of allowed) {
		const old = previous.get(key);
		if (old && !next.has(key)) next.set(key, old);
	}
	return next;
}

/** The folder keys accepted empty, with `key` added or removed: a new set, in the order they were accepted. */
export function toggleEmptyFolder(allowed: ReadonlySet<string>, key: string, on: boolean): Set<string> {
	const next = new Set(allowed);
	if (on) next.add(key);
	else next.delete(key);
	return next;
}

/** The percentage that would let a refused deletion through, as a whole number from 1 to 100; null when the block does not say (or says nonsense). */
export function neededPercent(block: Pick<MirrorBlock, 'code' | 'needed_percent'>): number | null {
	const n = block.needed_percent;
	if (block.code !== 'too_many_deletes' || typeof n !== 'number' || !Number.isFinite(n)) return null;
	return Math.min(100, Math.max(1, Math.ceil(n)));
}

/** What a mirror run does that is worth knowing, without stopping it. The API message of each note is kept as the detail. */
export const NOTE_CODES = ['soft_delete', 'hard_delete', 'no_copy_kept', 'shared_resource', 'target_modified', 'unverified_owner'] as const;
export type NoteCode = (typeof NOTE_CODES)[number];

/** `warning` is shown before Apply as something to read (a deletion that cannot be undone, or less safe than usual); `note` is plain information. */
export type NoteTone = 'warning' | 'note';

export const noteTone = (code: string): NoteTone => (code === 'soft_delete' || code === 'hard_delete' ? 'warning' : 'note');

/** The wording of the interface for the codes it knows: a short label and a sentence of explanation. */
export type NoteLabels = Record<NoteCode, { label: string; hint: string }>;

export interface NoteLine {
	code: string;
	tone: NoteTone;
	/** The label of a known code; for an unknown one, the message of the API as is. */
	label: string;
	/** The explanation of a known code. */
	hint: string | null;
	/** The message of the API, when it adds something to the label. */
	detail: string | null;
}

export const isNoteCode = (code: string): code is NoteCode => (NOTE_CODES as readonly string[]).includes(code);

/** One note as it is shown. An unknown code (a newer server) shows its API message in place of a label, or the code itself when it has none. */
export function describeNote(note: MirrorNote, labels: NoteLabels): NoteLine {
	const message = (note.message ?? '').trim();
	if (!isNoteCode(note.code)) return { code: note.code, tone: 'note', label: message || note.code, hint: null, detail: null };
	return { code: note.code, tone: noteTone(note.code), label: labels[note.code].label, hint: labels[note.code].hint, detail: message || null };
}

/** The notes of a list, warnings first, then in the order of the API; a code repeated with the same message is shown once. */
export function noteLines(notes: readonly MirrorNote[] | null | undefined, labels: NoteLabels): NoteLine[] {
	const seen = new Set<string>();
	const lines: NoteLine[] = [];
	for (const note of notes ?? []) {
		if (!note || typeof note.code !== 'string') continue;
		const key = `${note.code}\u0000${note.message ?? ''}`;
		if (seen.has(key)) continue;
		seen.add(key);
		lines.push(describeNote(note, labels));
	}
	return lines.map((line, index) => ({ line, index })).sort((a, b) => Number(b.line.tone === 'warning') - Number(a.line.tone === 'warning') || a.index - b.index).map((item) => item.line);
}

/** A soft or permanent deletion is announced: the mailbox is opened on it, whatever the size of the list. */
export const hasWarningNote = (notes: readonly MirrorNote[] | null | undefined): boolean => (notes ?? []).some((n) => noteTone(n?.code) === 'warning');

/** A percentage typed by hand, brought to a whole number from 1 to 100; the default when it is not a number. */
export function clampPercent(value: number | string): number {
	const text = typeof value === 'number' ? String(value) : value.trim().replace(',', '.');
	const n = text === '' ? NaN : Number(text);
	if (!Number.isFinite(n)) return DEFAULT_MAX_DELETE_PERCENT;
	return Math.min(100, Math.max(1, Math.round(n)));
}

/** The minimal number of deletions typed by hand, brought to a whole number from 0; the default when it is not a number. */
export function clampCount(value: number | string): number {
	const text = typeof value === 'number' ? String(value) : value.trim().replace(',', '.');
	const n = text === '' ? NaN : Number(text);
	if (!Number.isFinite(n)) return DEFAULT_MIN_DELETE_COUNT;
	return Math.min(MAX_MIN_DELETE_COUNT, Math.max(0, Math.round(n)));
}

/** The jobs a mirror run created. A job given back as it is (`existing`, one that was already active) is not one of them. */
export const startedJobs = (jobs: readonly Job[]): Job[] => jobs.filter((job) => !job.existing);

export interface MirrorReportRow {
	kind: MirrorKind;
	added: number;
	updated: number;
	deleted: number;
	skipped: number;
	errors: number;
}

/** The mirror section of a job report, or null when the job was not a mirror run (the report has none). */
export function mirrorReport(report: Record<string, unknown> | null | undefined): MirrorReport | null {
	const value = report?.mirror;
	return value && typeof value === 'object' && !Array.isArray(value) ? (value as MirrorReport) : null;
}

/** One line per kind that the mirror run touched, left out or failed on. */
export function mirrorReportRows(report: MirrorReport): MirrorReportRow[] {
	const rows: MirrorReportRow[] = [];
	for (const kind of MIRROR_KINDS) {
		const own = report[kind];
		const row = { kind, added: own?.added ?? 0, updated: own?.updated ?? 0, deleted: own?.deleted ?? 0, skipped: own?.skipped_by_safety ?? 0, errors: own?.errors ?? 0 };
		if (row.added || row.updated || row.deleted || row.skipped || row.errors) rows.push(row);
	}
	return rows;
}

export const mirrorSkipped = (report: MirrorReport): number => MIRROR_KINDS.reduce((n, kind) => n + (report[kind]?.skipped_by_safety ?? 0), 0);

/** A mirror resync is offered when there is no job or the last one is over: a queued, running or paused one is left alone. */
export const canMirrorResync = (job: Pick<Job, 'status'> | undefined): boolean => !job || ['done', 'done_with_errors', 'stopped', 'failed'].includes(job.status);
