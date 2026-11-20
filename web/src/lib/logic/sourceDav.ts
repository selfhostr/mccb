import type { Account, Server, SourceDavProblem } from '../api/types.ts';
import { hasOwnTarget } from './connector.ts';

/** Why calendars or contacts would stay at the source: a problem of its server, or 'kind' (that kind of source only carries mail). */
export type SourceGap = SourceDavProblem | 'kind';
export type ContentKind = 'calendar' | 'contacts';

export interface GapGroup {
	gap: SourceGap;
	/** The saved server the IMAP source is on, when known (the fix is there: one click for all its mailboxes). */
	serverId: number | null;
	accounts: Account[];
}

/**
 * What keeps this kind at the source, null when the source brings it or when it cannot be known (PST, files).
 * The DAV switch is read from the servers as they are now: turning it on clears the gap without reloading the mailboxes.
 */
export function sourceGap(account: Pick<Account, 'source_carries' | 'source_dav'>, kind: ContentKind, serverOf: (id: number) => Pick<Server, 'dav_enabled'> | undefined): SourceGap | null {
	const carries = account.source_carries;
	if (!carries) return null;
	const dav = account.source_dav;
	if (!dav) return carries[kind] ? null : 'kind';
	if ((dav.problem === null || dav.problem === 'dav_off') && dav.server_id != null) {
		const server = serverOf(dav.server_id);
		if (server) return server.dav_enabled ? null : 'dav_off';
	}
	return dav.problem;
}

/** The mailboxes whose calendars or contacts, wanted, would stay at the source, grouped by cause and server. */
export function sourceGaps(accounts: Account[], wanted: (account: Account, kind: ContentKind) => boolean, serverOf: (id: number) => Pick<Server, 'dav_enabled'> | undefined): GapGroup[] {
	const groups = new Map<string, GapGroup>();
	for (const account of accounts) {
		// An own target is another route: what it takes is said there.
		if (hasOwnTarget(account)) continue;
		const gap = (['calendar', 'contacts'] as const).map((k) => (wanted(account, k) ? sourceGap(account, k, serverOf) : null)).find((g) => g !== null) ?? null;
		if (!gap) continue;
		const serverId = account.source_dav?.server_id ?? null;
		const key = `${gap}:${serverId ?? ''}`;
		const group = groups.get(key) ?? { gap, serverId, accounts: [] };
		group.accounts.push(account);
		groups.set(key, group);
	}
	return [...groups.values()];
}
