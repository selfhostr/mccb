import { describe, expect, it } from 'vitest';
import type { Coverage, MatchRow, MatchState, TenantMailbox } from '../../src/lib/api/types.ts';
import {
	addressesOf,
	baseRows,
	bestServer,
	canFinalResync,
	countStates,
	coverageSummary,
	filterRows,
	choiceFor,
	hiddenTicked,
	isManual,
	isTickable,
	landingKinds,
	linksOf,
	parseStateFilter,
	pickChoices,
	pretick,
	rowKey,
	searchChoices,
	sortRows,
	targetRows,
	tasksAllowed,
	tickHeader,
	tickReady,
	toggleVisible
} from '../../src/lib/logic/tenantmatch.ts';

const FULL: Coverage = { mail: 'on', calendar: 'on', contacts: 'on', tasks: 'on' };

function box(id: string, mail: string, extra: Partial<TenantMailbox> = {}): TenantMailbox {
	return { id, display_name: id.toUpperCase(), mail, upn: mail, aliases: [], kind: 'user', enabled: true, licensed: true, ...extra };
}

let nextAccount = 1;
function row(state: MatchState, name: string, extra: Partial<MatchRow> = {}): MatchRow {
	const mail = `${name}@contoso.example`;
	const mailbox = state === 'no_source' ? null : box(name, mail);
	const account =
		state === 'no_target'
			? null
			: { id: nextAccount++, email: mail, status: 'ok' as const, last_test: null, last_test_result: null, source_kind: null, source_connection_id: null };
	return { state, mailbox, account, matched_by: mailbox && account ? 'mail' : null, matched_address: null, coverage: FULL, last_job: null, ...extra };
}

const ROWS = [
	row('ready', 'anna'),
	row('ready', 'bob'),
	row('linked', 'carla'),
	row('no_target', 'dan'),
	row('no_source', 'eve'),
	row('other_source', 'fay'),
	row('conflict', 'gus'),
	row('ready', 'room1', { mailbox: box('room1', 'room1@contoso.example', { kind: 'room' }) })
];

describe('tenant matching: rows', () => {
	it('only full ready or linked pairs are tickable', () => {
		expect(ROWS.filter(isTickable).map((r) => r.mailbox?.id)).toEqual(['anna', 'bob', 'carla', 'room1']);
	});

	it('keys pair the two sides and stay unique', () => {
		const keys = ROWS.map(rowKey);
		expect(new Set(keys).size).toBe(keys.length);
		expect(rowKey({ mailbox: null, account: { id: 7 } as MatchRow['account'] })).toBe('-|7');
	});

	it('a state filter in the URL is checked', () => {
		expect(parseStateFilter('linked')).toBe('linked');
		expect(parseStateFilter('nope')).toBe('');
		expect(parseStateFilter(null)).toBe('');
	});

	it('sorts by state then name', () => {
		const sorted = sortRows([ROWS[6], ROWS[1], ROWS[3], ROWS[0]]);
		expect(sorted.map((r) => r.mailbox?.id)).toEqual(['anna', 'bob', 'dan', 'gus']);
	});
});

describe('tenant matching: filters and counters', () => {
	it('rooms are hidden unless asked for, and the counters follow', () => {
		expect(countStates(baseRows(ROWS, { q: '', rooms: false }))).toEqual({ all: 7, ready: 2, linked: 1, other_source: 1, no_target: 1, no_source: 1, conflict: 1 });
		expect(countStates(baseRows(ROWS, { q: '', rooms: true })).ready).toBe(3);
	});

	it('search looks at name, every address of the mailbox and the account address', () => {
		const withAlias = row('ready', 'hal', { mailbox: box('hal', 'hal@contoso.example', { display_name: 'Hal Jordan', aliases: ['green@old.example'] }) });
		const rows = [...ROWS, withAlias];
		expect(filterRows(rows, { state: '', q: 'GREEN@', rooms: false })).toEqual([withAlias]);
		expect(filterRows(rows, { state: '', q: 'jordan', rooms: false })).toEqual([withAlias]);
		expect(filterRows(rows, { state: '', q: 'eve@', rooms: false }).map((r) => r.state)).toEqual(['no_source']);
	});

	it('the state chip filters after search; counters ignore the state', () => {
		const filter = { state: 'ready' as const, q: 'an', rooms: false };
		expect(filterRows(ROWS, filter).map((r) => r.mailbox?.id)).toEqual(['anna']);
		expect(countStates(baseRows(ROWS, filter))).toMatchObject({ all: 2, ready: 1, no_target: 1 });
	});
});

describe('tenant matching: ticks', () => {
	it('ready rows start ticked, linked ones do not', () => {
		const ticked = pretick(ROWS);
		expect(ROWS.filter((r) => ticked.has(rowKey(r))).map((r) => r.mailbox?.id)).toEqual(['anna', 'bob', 'room1']);
	});

	it('a refresh keeps the choices made and ticks only new ready rows', () => {
		const first = pretick(ROWS);
		first.delete(rowKey(ROWS[0]));
		first.add(rowKey(ROWS[2]));
		const fresh = row('ready', 'ivy');
		const next = pretick([...ROWS, fresh], { ticked: first, known: new Set(ROWS.map(rowKey)) });
		expect(next.has(rowKey(ROWS[0]))).toBe(false);
		expect(next.has(rowKey(ROWS[2]))).toBe(true);
		expect(next.has(rowKey(fresh))).toBe(true);
	});

	it('a tick is dropped when its row is no longer tickable', () => {
		const ticked = new Set([rowKey(ROWS[0])]);
		const now = [{ ...ROWS[0], state: 'other_source' as const }];
		expect(pretick(now, { ticked, known: ticked }).size).toBe(0);
	});

	it('actions touch visible ticked rows only; hidden ones are counted', () => {
		const ticked = pretick(ROWS);
		const visible = filterRows(ROWS, { state: '', q: '', rooms: false });
		expect(targetRows(visible, ticked).map((r) => r.mailbox?.id)).toEqual(['anna', 'bob']);
		expect(hiddenTicked(ROWS, visible, ticked)).toBe(1);
	});

	it('the header ticks every visible tickable row, then unticks them', () => {
		const visible = ROWS.slice(0, 4);
		expect(tickHeader(visible, new Set())).toBe('none');
		const all = toggleVisible(visible, new Set([rowKey(ROWS[0])]));
		expect(tickHeader(visible, all)).toBe('all');
		expect([...all].sort()).toEqual([ROWS[0], ROWS[1], ROWS[2]].map(rowKey).sort());
		expect(toggleVisible(visible, all).size).toBe(0);
		expect(tickHeader(ROWS.slice(3, 7), new Set())).toBe('none');
	});

	it('"tick all ready" adds ready rows and keeps the others', () => {
		const next = tickReady(ROWS.slice(0, 3), new Set([rowKey(ROWS[2])]));
		expect(next).toEqual(new Set([ROWS[0], ROWS[1], ROWS[2]].map(rowKey)));
	});

	it('final resync needs every ticked row linked', () => {
		expect(canFinalResync([])).toBe(false);
		expect(canFinalResync([ROWS[2]])).toBe(true);
		expect(canFinalResync([ROWS[2], ROWS[0]])).toBe(false);
	});
});

describe('tenant matching: coverage and start', () => {
	const noDav: Coverage = { mail: 'on', calendar: 'no_dav', contacts: 'no_dav', tasks: 'no_dav' };
	const partial = [row('ready', 'j', { coverage: noDav }), row('ready', 'k', { coverage: { ...FULL, tasks: 'no_permission', contacts: 'off' } })];

	it('sums each kind by state', () => {
		const s = coverageSummary(partial);
		expect(s.mail.on).toBe(2);
		expect(s.calendar).toMatchObject({ on: 1, no_dav: 1 });
		expect(s.contacts).toMatchObject({ on: 0, off: 1, no_dav: 1 });
		expect(s.tasks).toMatchObject({ no_dav: 1, no_permission: 1 });
	});

	it('the bottom bar names only what lands somewhere', () => {
		expect(landingKinds(partial)).toEqual(['mail', 'calendar']);
		expect(landingKinds([row('ready', 'l', { coverage: noDav })])).toEqual(['mail']);
		expect(landingKinds([ROWS[0]])).toEqual(['mail', 'calendar', 'contacts', 'tasks']);
	});

	it('tasks are allowed only when no row lacks the permission', () => {
		expect(tasksAllowed(ROWS)).toBe(true);
		expect(tasksAllowed(partial)).toBe(false);
	});

	it('links pair account and mailbox ids', () => {
		expect(linksOf([ROWS[0], ROWS[3]])).toEqual([{ account_id: ROWS[0].account!.id, mailbox_id: 'anna' }]);
	});
});

describe('tenant matching: default server', () => {
	const boxes = [box('a', 'a@contoso.example', { aliases: ['A@old.example'] }), box('b', 'b@contoso.example')];

	it('every address counts once, lower-cased', () => {
		expect(addressesOf({ mail: 'A@x.example', upn: 'a@x.example', aliases: ['B@x.example'] })).toEqual(['a@x.example', 'b@x.example']);
	});

	it('picks the server with the most matching accounts, the first one on a tie', () => {
		const servers = [{ id: 1 }, { id: 2 }];
		expect(bestServer(servers, [{ server_id: 2, email: 'a@old.example' }, { server_id: 2, email: 'b@contoso.example' }, { server_id: 1, email: 'a@contoso.example' }], boxes)).toBe(2);
		expect(bestServer(servers, [], boxes)).toBe(1);
		expect(bestServer([], [], boxes)).toBeNull();
	});
});

describe('tenant matching: a destination chosen by hand', () => {
	const conflictBox = row('conflict', 'hal');
	const rows = [...ROWS, row('no_source', 'kit', { account: { ...row('no_source', 'kit').account!, free: false } }), { ...conflictBox, mailbox: box('ivy', 'ivy@contoso.example'), account: ROWS[6].account }];
	const choices = pickChoices(rows, 'anna');
	const block = (name: string) => choices.find((c) => c.account.email.startsWith(`${name}@`))?.block;

	it('lists every box of the server once, free ones first', () => {
		const ids = choices.map((c) => c.account.id);
		expect(new Set(ids).size).toBe(ids.length);
		expect(choices.length).toBe(rows.filter((r) => r.account).length - 1); // gus appears in two conflict rows
		expect(choices.slice(0, 2).map((c) => c.block)).toEqual([null, null]);
		expect(choices.map((c) => c.account.email.split('@')[0]).slice(0, 2)).toEqual(['eve', 'gus']);
	});

	it('says why a box cannot be chosen', () => {
		expect(block('anna')).toBe('current');
		expect(block('bob')).toBe('taken');
		expect(choices.find((c) => c.account.email.startsWith('bob@'))?.takenBy).toBe('bob@contoso.example');
		expect(block('carla')).toBe('taken'); // linked to another mailbox
		expect(block('fay')).toBe('other_source');
		expect(block('kit')).toBe('other_source'); // free: false
		expect(block('gus')).toBeNull(); // a conflict is settled by a choice
		expect(block('eve')).toBeNull();
	});

	it('searches and finds a typed address', () => {
		expect(searchChoices(choices, ' EVE').map((c) => c.account.email)).toEqual(['eve@contoso.example']);
		expect(searchChoices(choices, '')).toHaveLength(choices.length);
		expect(choiceFor(choices, 'Bob@Contoso.example ')?.block).toBe('taken');
		expect(choiceFor(choices, 'test@yety.ch')).toBeUndefined();
	});

	it('a manual pair is a full pair matched by hand', () => {
		expect(isManual({ ...ROWS[0], matched_by: 'manual' })).toBe(true);
		expect(isManual(ROWS[0])).toBe(false);
		expect(isManual({ ...ROWS[3], matched_by: 'manual' })).toBe(false);
	});
});
