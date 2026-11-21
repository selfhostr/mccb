import { describe, expect, it } from 'vitest';
import type { Account } from '../../src/lib/api/types.ts';
import { sourceGap, sourceGaps } from '../../src/lib/logic/sourceDav.ts';

const servers = new Map([
	[7, { dav_enabled: false }],
	[8, { dav_enabled: true }]
]);
const serverOf = (id: number) => servers.get(id);
const acc = (id: number, over: Partial<Account>) => ({ id, email: `u${id}@example.com`, server_id: 1, target: { kind: null, config: {}, has_secret: false }, options: { mail: true, calendar: true, contacts: true }, ...over }) as Account;

describe('calendars and contacts the source cannot bring', () => {
	it('unknown for a PST, nothing to say', () => {
		expect(sourceGap({ source_carries: null, source_dav: null }, 'calendar', serverOf)).toBeNull();
	});

	it('a source kind without them, and one that has them', () => {
		expect(sourceGap({ source_carries: { calendar: false, contacts: true }, source_dav: null }, 'calendar', serverOf)).toBe('kind');
		expect(sourceGap({ source_carries: { calendar: false, contacts: true }, source_dav: null }, 'contacts', serverOf)).toBeNull();
	});

	it('an IMAP source follows the DAV switch of its server as it is now', () => {
		const stale = { source_carries: { calendar: true, contacts: true }, source_dav: { server_id: 7, problem: null } };
		expect(sourceGap(stale, 'calendar', serverOf)).toBe('dav_off');
		const turnedOn = { source_carries: { calendar: false, contacts: false }, source_dav: { server_id: 8, problem: 'dav_off' as const } };
		expect(sourceGap(turnedOn, 'contacts', serverOf)).toBeNull();
		expect(sourceGap({ source_carries: { calendar: false, contacts: false }, source_dav: { server_id: null, problem: 'no_server' } }, 'calendar', serverOf)).toBe('no_server');
		expect(sourceGap({ source_carries: { calendar: false, contacts: false }, source_dav: { server_id: 8, problem: 'ambiguous' } }, 'calendar', serverOf)).toBe('ambiguous');
	});

	it('groups the mailboxes by cause and server, only for what is wanted, never for an own target', () => {
		const imapOff = { source_carries: { calendar: false, contacts: false }, source_dav: { server_id: 7, problem: 'dav_off' as const } };
		const list = [
			acc(1, imapOff),
			acc(2, imapOff),
			acc(3, { source_carries: { calendar: false, contacts: false }, source_dav: { server_id: null, problem: 'no_server' } }),
			acc(4, { ...imapOff, target: { kind: 'ews', config: {}, has_secret: true } }),
			acc(5, { source_carries: null, source_dav: null })
		];
		const groups = sourceGaps(list, (_a, k) => k === 'contacts', serverOf);
		expect(groups.map((g) => [g.gap, g.serverId, g.accounts.map((a) => a.id)])).toEqual([
			['dav_off', 7, [1, 2]],
			['no_server', null, [3]]
		]);
		expect(sourceGaps(list, () => false, serverOf)).toEqual([]);
	});
});
