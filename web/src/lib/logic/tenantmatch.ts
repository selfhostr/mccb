/**
 * Matching screen (Microsoft 365 tenant -> server), the pure part: filters, counters, ticks, what a start covers.
 * Rows come from the server (GET /source-connections/{id}/match); nothing here decides a match.
 */
import type { CoverageState, MatchRow, MatchState, TenantMailbox } from '../api/types.ts';

export type CoverageKind = 'mail' | 'calendar' | 'contacts' | 'tasks';
export const COVERAGE_KINDS: readonly CoverageKind[] = ['mail', 'calendar', 'contacts', 'tasks'];

/** Chip order, also the order of the rows: what can go first, then what needs a look. */
export const STATE_ORDER: readonly MatchState[] = ['ready', 'linked', 'no_target', 'no_source', 'other_source', 'conflict'];

export type StateFilter = MatchState | '';

export function parseStateFilter(value: string | null): StateFilter {
	return value && (STATE_ORDER as readonly string[]).includes(value) ? (value as MatchState) : '';
}

/** Rows have no id of their own: the pair is the key. */
export function rowKey(row: Pick<MatchRow, 'mailbox' | 'account'>): string {
	return `${row.mailbox?.id ?? '-'}|${row.account?.id ?? '-'}`;
}

/** Only a full pair that is free or already ours can be started. */
export function isTickable(row: Pick<MatchRow, 'state' | 'mailbox' | 'account'>): boolean {
	return (row.state === 'ready' || row.state === 'linked') && !!row.mailbox && !!row.account;
}

export function isResource(row: Pick<MatchRow, 'mailbox'>): boolean {
	return row.mailbox?.kind === 'room' || row.mailbox?.kind === 'equipment';
}

export function matchesSearch(row: Pick<MatchRow, 'mailbox' | 'account'>, q: string): boolean {
	const needle = q.trim().toLowerCase();
	if (!needle) return true;
	const box = row.mailbox;
	const hay = [box?.display_name, box?.mail, box?.upn, ...(box?.aliases ?? []), row.account?.email];
	return hay.some((v) => !!v && v.toLowerCase().includes(needle));
}

export interface MatchFilter {
	state: StateFilter;
	q: string;
	/** Rooms and equipment are hidden unless asked for. */
	rooms: boolean;
}

/** What the chips count: search and the rooms toggle apply, the state does not. */
export function baseRows<R extends MatchRow>(rows: readonly R[], filter: Pick<MatchFilter, 'q' | 'rooms'>): R[] {
	return rows.filter((r) => (filter.rooms || !isResource(r)) && matchesSearch(r, filter.q));
}

export function filterRows<R extends MatchRow>(rows: readonly R[], filter: MatchFilter): R[] {
	return baseRows(rows, filter).filter((r) => !filter.state || r.state === filter.state);
}

export type StateCounts = Record<MatchState | 'all', number>;

export function countStates(rows: readonly Pick<MatchRow, 'state'>[]): StateCounts {
	const c: StateCounts = { all: rows.length, ready: 0, linked: 0, other_source: 0, no_target: 0, no_source: 0, conflict: 0 };
	for (const r of rows) if (r.state in c) c[r.state]++;
	return c;
}

const nameOf = (r: MatchRow) => (r.mailbox?.display_name || r.mailbox?.mail || r.account?.email || '').toLowerCase();

export function sortRows<R extends MatchRow>(rows: readonly R[]): R[] {
	const rank = (s: MatchState) => {
		const i = STATE_ORDER.indexOf(s);
		return i < 0 ? STATE_ORDER.length : i;
	};
	return [...rows].sort((a, b) => rank(a.state) - rank(b.state) || nameOf(a).localeCompare(nameOf(b)));
}

export interface TickMemory {
	ticked: ReadonlySet<string>;
	/** Every row key seen before: a ready row seen and unticked stays unticked on refresh. */
	known: ReadonlySet<string>;
}

/**
 * Ready rows start ticked. On a refresh of the same screen, the choices made stay: a tick survives while its row
 * is still tickable, a new ready row comes in ticked, an unticked one is not ticked again.
 */
export function pretick(rows: readonly MatchRow[], previous?: TickMemory): Set<string> {
	const next = new Set<string>();
	for (const row of rows) {
		if (!isTickable(row)) continue;
		const key = rowKey(row);
		if (previous?.known.has(key)) {
			if (previous.ticked.has(key)) next.add(key);
		} else if (row.state === 'ready') next.add(key);
	}
	return next;
}

/** Ticked, visible and tickable: the only rows an action touches (see logic/selection.ts). */
export function targetRows<R extends MatchRow>(visible: readonly R[], ticked: ReadonlySet<string>): R[] {
	return visible.filter((r) => isTickable(r) && ticked.has(rowKey(r)));
}

/** Ticked rows a filter hides: counted and said, never acted on. */
export function hiddenTicked(all: readonly MatchRow[], visible: readonly MatchRow[], ticked: ReadonlySet<string>): number {
	const shown = new Set(visible.map(rowKey));
	let n = 0;
	for (const r of all) {
		const key = rowKey(r);
		if (ticked.has(key) && !shown.has(key) && isTickable(r)) n++;
	}
	return n;
}

export type TickHeader = 'none' | 'some' | 'all';

export function tickHeader(visible: readonly MatchRow[], ticked: ReadonlySet<string>): TickHeader {
	const tickable = visible.filter(isTickable);
	if (!tickable.length) return 'none';
	const hit = tickable.filter((r) => ticked.has(rowKey(r))).length;
	return hit === 0 ? 'none' : hit === tickable.length ? 'all' : 'some';
}

/** Header checkbox: "all" unticks the visible rows, anything else ticks every visible tickable row. */
export function toggleVisible(visible: readonly MatchRow[], ticked: ReadonlySet<string>): Set<string> {
	const state = tickHeader(visible, ticked);
	const next = new Set(ticked);
	for (const r of visible) {
		if (!isTickable(r)) continue;
		if (state === 'all') next.delete(rowKey(r));
		else next.add(rowKey(r));
	}
	return next;
}

/** "Tick all ready": adds the visible ready rows, keeps the rest as it is. */
export function tickReady(visible: readonly MatchRow[], ticked: ReadonlySet<string>): Set<string> {
	const next = new Set(ticked);
	for (const r of visible) if (r.state === 'ready' && isTickable(r)) next.add(rowKey(r));
	return next;
}

/** The final resync (delta) is for mailboxes already migrated from this tenant, and only those. */
export function canFinalResync(targets: readonly Pick<MatchRow, 'state'>[]): boolean {
	return targets.length > 0 && targets.every((r) => r.state === 'linked');
}

/** Per kind, how many of the rows will get it, and how many will not and why. */
export type CoverageSummary = Record<CoverageKind, Record<CoverageState, number>>;

export function coverageSummary(rows: readonly Pick<MatchRow, 'coverage'>[]): CoverageSummary {
	const empty = (): Record<CoverageState, number> => ({ on: 0, off: 0, no_dav: 0, failed: 0, unknown: 0, no_permission: 0 });
	const out = { mail: empty(), calendar: empty(), contacts: empty(), tasks: empty() };
	for (const r of rows) {
		for (const kind of COVERAGE_KINDS) {
			const state = r.coverage?.[kind] ?? 'unknown';
			if (state in out[kind]) out[kind][state]++;
		}
	}
	return out;
}

/** Kinds at least one row will receive: the words of the bottom bar ("mail, calendars ... to <server>"). */
export function landingKinds(rows: readonly Pick<MatchRow, 'coverage'>[]): CoverageKind[] {
	const summary = coverageSummary(rows);
	return COVERAGE_KINDS.filter((k) => summary[k].on > 0);
}

export function linksOf(rows: readonly MatchRow[]): { account_id: number; mailbox_id: string }[] {
	const out: { account_id: number; mailbox_id: string }[] = [];
	for (const r of rows) if (r.account && r.mailbox) out.push({ account_id: r.account.id, mailbox_id: r.mailbox.id });
	return out;
}

/** Tasks go only when the tenant granted the permission: no row may be asked for tasks otherwise. */
export function tasksAllowed(rows: readonly Pick<MatchRow, 'coverage'>[]): boolean {
	return !rows.some((r) => r.coverage?.tasks === 'no_permission');
}

/** Every address that names a mailbox, lower-cased, without duplicates. */
export function addressesOf(box: Pick<TenantMailbox, 'mail' | 'upn' | 'aliases'>): string[] {
	const set = new Set<string>();
	for (const v of [box.mail, box.upn, ...(box.aliases ?? [])]) if (v) set.add(v.toLowerCase());
	return [...set];
}

/**
 * Default target: the server holding the most accounts whose address is one of the tenant's. Ties keep the
 * order of the server list; no server at all gives null.
 */
export function bestServer(servers: readonly { id: number }[], accounts: Iterable<{ server_id: number; email: string }>, mailboxes: readonly TenantMailbox[]): number | null {
	if (!servers.length) return null;
	const addresses = new Set<string>();
	for (const box of mailboxes) for (const a of addressesOf(box)) addresses.add(a);
	const hits = new Map<number, number>();
	for (const acc of accounts) if (addresses.has(acc.email.toLowerCase())) hits.set(acc.server_id, (hits.get(acc.server_id) ?? 0) + 1);
	let best = servers[0].id;
	let most = hits.get(best) ?? 0;
	for (const s of servers) {
		const n = hits.get(s.id) ?? 0;
		if (n > most) {
			best = s.id;
			most = n;
		}
	}
	return best;
}
