import { describe, expect, it } from 'vitest';
import type { Account, Connector, ConnectorRole, Job, Pst } from '../../src/lib/api/types.ts';
import { importPlan, matchesFilter } from '../../src/lib/logic/accounts.ts';
import {
	buildConfig,
	buildSecrets,
	endpointBody,
	endpointSummary,
	hasCustomSource,
	initialSecrets,
	initialValues,
	kindLabel,
	kindsFor,
	missingRequired
} from '../../src/lib/logic/connector.ts';
import { buildFetchBody, missingFetchFields } from '../../src/lib/logic/fetch.ts';
import { EVENT_GROUPS, isPublicNtfy, notificationsDirty, notificationsPatch } from '../../src/lib/logic/notify.ts';
import { chainView, fetchPercent, isFetching } from '../../src/lib/logic/pst.ts';

const field = (name: string, type: string, over: Record<string, unknown> = {}) => ({ name, label: name, type, required: false, secret: false, default: null, ...over });
const role: ConnectorRole = {
	items: ['mail'],
	capabilities: [],
	fields: [field('host', 'text', { required: true }), field('port', 'number', { default: 993 }), field('tls', 'boolean', { default: true }), field('password', 'password', { secret: true, required: true }), field('note', 'text')]
};
const catalogue: Connector[] = [
	{ kind: 'imap', label: 'IMAP', description: '', source: role, target: role },
	{ kind: 'pst', label: 'PST file', description: '', source: { items: [], capabilities: [], fields: [] }, target: null },
	{ kind: 'caldav', label: 'CalDAV', description: '', source: null, target: role }
];

describe('connector catalogue', () => {
	it('lists the kinds that have the role, without the PST as a source', () => {
		expect(kindsFor(catalogue, 'source').map((c) => c.kind)).toEqual(['imap']);
		expect(kindsFor(catalogue, 'target').map((c) => c.kind)).toEqual(['imap', 'caldav']);
	});

	it('names an unknown kind by itself', () => {
		expect(kindLabel(catalogue, 'imap')).toBe('IMAP');
		expect(kindLabel(catalogue, 'jmap')).toBe('jmap');
	});

	it('starts from stored values then defaults, never from a secret', () => {
		expect(initialValues(role, { host: 'mail.example.com' })).toEqual({ host: 'mail.example.com', port: 993, tls: true, note: '' });
		expect(initialSecrets(role)).toEqual({ password: { value: '', clear: false } });
	});

	it('sends numbers as numbers and leaves empty text to the server defaults', () => {
		expect(buildConfig(role, { host: ' mail.example.com ', port: '143', tls: false, note: '' })).toEqual({ host: 'mail.example.com', port: 143, tls: false });
		expect(buildConfig(role, { host: 'h', port: null })).toEqual({ host: 'h' });
	});

	it('omits untouched secrets, replaces typed ones, clears on request', () => {
		expect(buildSecrets({ password: { value: '', clear: false } })).toBeUndefined();
		expect(buildSecrets({ password: { value: 'x', clear: false } })).toEqual({ password: 'x' });
		expect(buildSecrets({ password: { value: '', clear: true } })).toEqual({ password: '' });
		expect(endpointBody('imap', role, { host: 'h' }, { password: { value: '', clear: false } })).toEqual({ kind: 'imap', config: { host: 'h' } });
	});

	it('knows which required fields are missing, a stored secret counting as filled', () => {
		const drafts = initialSecrets(role);
		expect(missingRequired(role, { host: '' }, drafts, false)).toEqual(['host', 'password']);
		expect(missingRequired(role, { host: 'h' }, drafts, true)).toEqual([]);
		expect(missingRequired(role, { host: 'h' }, { password: { value: '', clear: true } }, true)).toEqual(['password']);
		expect(missingRequired(role, { host: 'h' }, { password: { value: 'x', clear: false } }, false)).toEqual([]);
	});

	it('summarises an endpoint in a few words, and a PST source as nothing', () => {
		expect(endpointSummary(catalogue, { kind: 'imap', config: { host: 'mail.example.com', port: 993 }, has_secret: true }, 'source')).toEqual({ kind: 'imap', label: 'IMAP', detail: 'mail.example.com' });
		expect(endpointSummary(catalogue, { kind: 'pst', config: { pst_id: 1 }, has_secret: false }, 'source')).toBeNull();
		expect(endpointSummary(catalogue, { kind: null, config: {}, has_secret: false }, 'target')).toBeNull();
		// A kind the catalogue does not know still gets a line.
		expect(endpointSummary(catalogue, { kind: 'jmap', config: { url: 'https://j.example.com' }, has_secret: false }, 'source')).toEqual({ kind: 'jmap', label: 'jmap', detail: 'https://j.example.com' });
	});
});

const account = (over: Partial<Account> = {}): Account =>
	({ id: 1, server_id: 1, email: 'a@example.com', login: null, has_password: true, status: 'ok', last_test: null, last_test_result: null, pst_id: null, options: { mail: true, calendar: true, contacts: true }, job: null, ...over }) as Account;

describe('accounts with a connector source', () => {
	const imapSource = { kind: 'imap', config: { host: 'h' }, has_secret: true };
	it('a connector source needs no PST', () => {
		const a = account({ source: imapSource });
		expect(hasCustomSource(a)).toBe(true);
		expect(hasCustomSource(account({ source: { kind: 'pst', config: {}, has_secret: false } }))).toBe(false);
		const plan = importPlan([a], () => undefined as Job | undefined, () => undefined as Pst | undefined);
		expect(plan.ready).toHaveLength(1);
		expect(plan.noPst).toHaveLength(0);
	});

	it('without a PST nor a source, the account is still reported', () => {
		const plan = importPlan([account()], () => undefined, () => undefined);
		expect(plan.noPst).toHaveLength(1);
	});

	it('an own target with its credentials replaces the account password', () => {
		const a = account({ has_password: false, source: imapSource, target: { kind: 'imap', config: {}, has_secret: true } });
		expect(importPlan([a], () => undefined, () => undefined).ready).toHaveLength(1);
		expect(importPlan([account({ has_password: false, source: imapSource })], () => undefined, () => undefined).noPassword).toHaveLength(1);
	});

	it('"no PST" does not list accounts that read from a connector', () => {
		const f = { q: '', server: null, status: 'no_pst' as const };
		expect(matchesFilter(account({ source: imapSource }), undefined, f)).toBe(false);
		expect(matchesFilter(account(), undefined, f)).toBe(true);
	});
});

describe('server-side fetch', () => {
	it('asks for the required fields of each kind', () => {
		expect(missingFetchFields('http', {})).toEqual(['url']);
		expect(missingFetchFields('http', { url: 'https://x/y.pst' })).toEqual([]);
		expect(missingFetchFields('s3', { endpoint: 'e', bucket: 'b' })).toEqual(['key', 'access_key', 'secret_key']);
	});

	it('SFTP needs a password or a key', () => {
		const base = { host: 'h', path: '/p', username: 'u' };
		expect(missingFetchFields('sftp', base)).toEqual(['password']);
		expect(missingFetchFields('sftp', { ...base, password: 'x' })).toEqual([]);
		expect(missingFetchFields('sftp', { ...base, private_key: '-----BEGIN' })).toEqual([]);
	});

	it('builds a body without empty fields, with a numeric port', () => {
		expect(buildFetchBody('sftp', { host: ' h ', path: '/p', username: 'u', password: 'p', port: '2222', passphrase: '', filename: '' })).toEqual({ kind: 'sftp', host: 'h', path: '/p', username: 'u', password: 'p', port: 2222 });
		expect(buildFetchBody('http', { url: 'https://x/y.pst', token: 't' })).toEqual({ kind: 'http', url: 'https://x/y.pst', token: 't' });
	});
});

describe('PST rows', () => {
	it('computes fetch progress only when the total is known', () => {
		expect(fetchPercent({ size: 0, received_bytes: 500 })).toBeNull();
		expect(fetchPercent({ size: 1000, received_bytes: 250 })).toBe(25);
		expect(fetchPercent({ size: 1000, received_bytes: 5000 })).toBe(100);
		expect(fetchPercent({ size: 1000 })).toBe(0);
		expect(isFetching({ status: 'cancelling' })).toBe(true);
		expect(isFetching({ status: 'fetch_failed' })).toBe(false);
	});

	it('puts the assignment outcome into a code, keeping unknown results', () => {
		const who = (id: number) => (id === 3 ? { email: 'jane@example.com' } : undefined);
		expect(chainView(null, who)).toBeNull();
		expect(chainView({ result: 'assigned', account_id: 3, job_id: 9 }, who)).toMatchObject({ code: 'assigned', accountEmail: 'jane@example.com', started: true });
		expect(chainView({ result: 'assigned', account_id: 4 }, who)).toMatchObject({ accountEmail: null, accountId: 4, started: false });
		expect(chainView({ result: 'error', error: 'boom' }, who)).toMatchObject({ code: 'error', detail: 'boom' });
		expect(chainView({ result: 'something_new' }, who)).toMatchObject({ code: 'other', detail: 'something_new' });
	});
});

describe('notifications', () => {
	const stored = {
		enabled: true,
		mask_addresses: false,
		group_window: 30,
		disk_low_mb: 5120,
		events: { upload_finished: false, analysis_valid: false, analysis_invalid: true, job_done: true, job_done_with_errors: true, job_failed: true, job_stopped: false, phase_mail: false, phase_calendar: false, phase_contacts: false, batch_finished: true, disk_low: true, repeated_crash: true },
		ntfy: { enabled: true, url: 'https://ntfy.sh', topic: 'abc', priority: 3, has_token: true },
		webhook: { enabled: false, url: '', has_token: false },
		email: { enabled: false, host: '', port: 587, security: 'starttls' as const, username: '', from: '', to: '', has_password: false }
	};
	const none = { ntfy: { value: '', clear: false }, webhook: { value: '', clear: false }, email: { value: '', clear: false } };

	it('every event belongs to one group, once', () => {
		const all = EVENT_GROUPS.flatMap((g) => g.events).sort();
		expect(all).toEqual(Object.keys(stored.events).sort());
	});

	it('recognises the public ntfy.sh service only', () => {
		expect(isPublicNtfy('https://ntfy.sh')).toBe(true);
		expect(isPublicNtfy('https://ntfy.sh/')).toBe(true);
		expect(isPublicNtfy('')).toBe(true);
		expect(isPublicNtfy('https://ntfy.example.org')).toBe(false);
		expect(isPublicNtfy('https://notntfy.sh.example.org')).toBe(false);
	});

	it('leaves secrets out unless touched, and never sends has_*', () => {
		const patch = notificationsPatch(stored, none);
		const n = patch.notifications as { ntfy: Record<string, unknown>; email: Record<string, unknown> };
		expect('token' in n.ntfy).toBe(false);
		expect('has_token' in n.ntfy).toBe(false);
		expect('password' in n.email).toBe(false);
		const touched = notificationsPatch(stored, { ...none, ntfy: { value: 'tk', clear: false }, email: { value: '', clear: true } });
		expect((touched.notifications as { ntfy: { token: string } }).ntfy.token).toBe('tk');
		expect((touched.notifications as { email: { password: string } }).email.password).toBe('');
	});

	it('is dirty on a changed field or a touched secret, not on a copy', () => {
		expect(notificationsDirty(structuredClone(stored), stored, none)).toBe(false);
		const changed = structuredClone(stored);
		changed.events.job_stopped = true;
		expect(notificationsDirty(changed, stored, none)).toBe(true);
		expect(notificationsDirty(stored, stored, { ...none, webhook: { value: 'x', clear: false } })).toBe(true);
	});
});
