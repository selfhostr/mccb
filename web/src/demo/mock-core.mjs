/**
 * The API of the UI, simulated: same contract as app/server/web.py (docs/api.md), fictional data, no Node API.
 * One module for two users, so that they cannot drift:
 *   - tests/ui/mock-api.mjs serves it over HTTP to the Playwright tests (password: admin);
 *   - src/demo/install.ts runs it in the browser for the static demo (npm run build:demo).
 * It uses only what Node and the browsers share: timers, Date, structuredClone, JSON.
 */
const CHUNK = 1024 * 1024;

/**
 * What the host adds. chunkOk(bytes, expectedSha256) -> boolean: checks an uploaded chunk (the tests do,
 * the demo does not read the bytes at all). version: what /api/version answers.
 */
export const hooks = { chunkOk: null, version: '0.0.0-mock' };

const listeners = new Set();
/** Receives every event of the stream as (kind, data). Returns the way to stop. */
export function subscribe(fn) {
	listeners.add(fn);
	return () => listeners.delete(fn);
}

let timers = new Set();
let state;

function emptyProgress() {
	return { mail: { done: 0, total: 0, errors: 0 }, calendar: { done: 0, total: 0, errors: 0 }, contacts: { done: 0, total: 0, errors: 0 } };
}

function makeAccount(id, email, serverId, over = {}) {
	return { id, server_id: serverId, email, login: email, has_password: true, status: 'new', last_test: null, last_test_result: null, pst_id: null, source: undefined, target: { kind: null, config: {}, has_secret: false }, options: { mail: true, calendar: true, contacts: true }, job: null, ...over };
}

// Same contract as app/connectors/registry.py catalogue(): the UI draws forms from it.
const field = (name, label, type = 'text', over = {}) => ({ name, label, type, required: false, secret: false, default: null, ...over });
const CATALOGUE = [
	{ kind: 'caldav', label: 'CalDAV', description: 'Calendars over CalDAV.', source: null, target: { items: ['calendar'], capabilities: [], fields: [field('url', 'Calendar URL', 'url', { required: true }), field('password', 'Password', 'password', { required: true, secret: true })] } },
	{
		kind: 'ews',
		label: 'Exchange (EWS)',
		description: 'Read a mailbox from Exchange Web Services.',
		source: { items: ['mail'], capabilities: ['folders'], fields: [field('server', 'Server URL', 'url', { required: true, help: 'Usually https://mail.example.com/EWS/Exchange.asmx' }), field('verify_tls', 'Verify the certificate', 'boolean', { default: true }), field('password', 'Password', 'password', { required: true, secret: true })] },
		target: null
	},
	{
		kind: 'future',
		label: 'Future mail service',
		description: 'A kind the UI has never heard of.',
		source: { items: ['mail'], capabilities: [], fields: [field('mailbox', 'Mailbox', 'text', { required: true }), field('mode', 'Mode', 'select', { options: ['fast', 'careful'], default: 'fast' }), field('batch', 'Batch size', 'number', { default: 50 }), field('weird', 'Strange field', 'hologram'), field('api_key', 'API key', 'password', { secret: true, required: true })] },
		target: null
	},
	{
		kind: 'imap',
		label: 'IMAP',
		description: 'Any IMAP mailbox.',
		source: { items: ['mail'], capabilities: ['folders'], fields: [field('host', 'Server', 'text', { required: true }), field('port', 'Port', 'number', { default: 993 }), field('security', 'Security', 'select', { options: ['ssl', 'starttls', 'none'], default: 'ssl' }), field('username', 'User', 'text', { required: true }), field('password', 'Password', 'password', { required: true, secret: true })] },
		target: { items: ['mail'], capabilities: ['parallel'], fields: [field('host', 'Server', 'text', { required: true }), field('port', 'Port', 'number', { default: 993 }), field('security', 'Security', 'select', { options: ['ssl', 'starttls', 'none'], default: 'ssl' }), field('username', 'User', 'text', { required: true }), field('password', 'Password', 'password', { required: true, secret: true })] }
	},
	{
		kind: 'm365_graph',
		label: 'Microsoft 365 (Graph)',
		description: 'Read mailboxes, calendars, contacts and tasks from Microsoft 365 through the Graph API.',
		directory: true,
		provision: true,
		source: {
			items: ['mail', 'calendar', 'contacts'],
			capabilities: ['folders'],
			fields: [
				field('tenant_id', 'Tenant ID', 'text', { required: true, scope: 'connection' }),
				field('client_id', 'Client ID', 'text', { required: true, scope: 'connection' }),
				field('client_secret', 'Client secret', 'password', { required: true, secret: true, scope: 'connection' }),
				field('cloud', 'Cloud', 'select', { options: ['global', 'usgov', 'china'], default: 'global', scope: 'connection' }),
				field('mailbox', 'Mailbox', 'text', { required: true, scope: 'mailbox' }),
				field('include_tasks', 'Include tasks', 'boolean', { default: true, scope: 'mailbox' })
			]
		},
		target: null
	},
	{ kind: 'pst', label: 'PST file', description: 'A Personal Folders file.', source: { items: ['mail', 'calendar', 'contacts'], capabilities: ['folders'], fields: [] }, target: null }
];

function defaultIntake() {
	return {
		notifications: {
			enabled: false,
			mask_addresses: false,
			group_window: 30,
			disk_low_mb: 5120,
			events: { upload_finished: false, analysis_valid: false, analysis_invalid: true, job_done: true, job_done_with_errors: true, job_failed: true, job_stopped: false, phase_mail: false, phase_calendar: false, phase_contacts: false, batch_finished: true, disk_low: true, repeated_crash: true },
			ntfy: { enabled: false, url: 'https://ntfy.sh', topic: '', priority: 3, has_token: false },
			webhook: { enabled: false, url: '', has_token: false },
			email: { enabled: false, host: '', port: 587, security: 'starttls', username: '', from: '', to: '', has_password: false }
		},
		inbox: { enabled: true, settle_seconds: 60, path: '/data/inbox' },
		auto_chain: { enabled: false, start_import: false, default_domains: {} }
	};
}

function makeServer(id, name, host, over = {}) {
	return { id, name, imap_host: host, imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: null, caldav_url: null, carddav_url: null, dav_enabled: true, workers: 4, notes: '', tuning: 'auto', builders: null, last_test: null, last_test_result: null, ...over };
}

function validPst(id, filename, size) {
	return {
		id,
		filename,
		size,
		status: 'valid',
		error: null,
		pst_sha: 'a'.repeat(64),
		created_at: Date.now() / 1000 - 3600,
		updated_at: Date.now() / 1000 - 3600,
		scan: { folders: [{ path: ['Inbox'], kind: 'mail', count: 900 }, { path: ['Calendar'], kind: 'calendar', count: 120 }, { path: ['Contacts'], kind: 'contacts', count: 80 }], totals: { mail: 900, calendar: 120, contacts: 80 }, errors: [] }
	};
}

/** @param {Record<string, any>} [options] */
export function reset(options = {}) {
	for (const t of timers) clearInterval(t);
	timers = new Set();
	const servers = options.servers === 0 ? [] : [makeServer(1, 'Production', 'imap.example.com'), makeServer(2, 'Staging', 'mail.home.arpa', { dav_enabled: false, imap_security: 'starttls', imap_port: 143 })];
	const psts = new Map([validPst(1, 'alice.pst', 2_000_000), validPst(2, 'bob.pst', 5_000_000)].map((p) => [p.id, p]));
	const accounts = new Map();
	const count = options.accounts ?? 6;
	if (count === 6 && options.servers !== 0) {
		const seed = ['alice@example.com', 'bob@example.com', 'carol@example.com', 'dave@example.com', 'erin@example.org', 'frank@example.org'];
		seed.forEach((email, i) => accounts.set(i + 1, makeAccount(i + 1, email, i < 4 ? 1 : 2, { pst_id: i < 3 ? (i % 2) + 1 : null })));
	} else if (options.servers !== 0) {
		for (let i = 1; i <= count; i++) accounts.set(i, makeAccount(i, `user${String(i).padStart(5, '0')}@example.com`, 1, { pst_id: 1 }));
	}
	state = {
		servers: new Map(servers.map((s) => [s.id, s])),
		accounts,
		psts,
		uploads: new Map(),
		jobs: new Map(),
		logs: new Map(),
		next: { server: 3, account: count + 1, pst: 3, job: 1 },
		testDelay: options.testDelay ?? 300,
		chunkDelay: options.chunkDelay ?? 0,
		jobTick: options.jobTick ?? 200,
		testCalls: {},
		runCalls: [],
		uploadOrder: [],
		intake: defaultIntake(),
		intakePuts: [],
		notifyTests: 0,
		inbox: options.inbox ?? [],
		routeCalls: [],
		probeCalls: [],
		fetchCalls: [],
		cancelCalls: [],
		retryCalls: [],
		runBodies: [],
		hold: options.hold ?? false,
		repair: { boxes: new Map(), runs: new Map(), next: { box: 1, run: 1 } },
		planDelay: options.planDelay ?? 600,
		plans: new Map(),
		compares: new Map(),
		batches: new Map(),
		sourceConnections: new Map(),
		tenantOn: false,
		sourceTests: [],
		linkCalls: [],
		tenantNext: 2,
		// "Connect with Microsoft": the flows in progress, and how the next one ends (null, 'admin', 'expired')
		provisionOn: options.provision !== false,
		provisionFail: options.provisionFail ?? null,
		flows: new Map(),
		flowNext: 1,
		provisionCalls: [],
		// set: what the tenant answers on listing its mailboxes (HTTP 502), as when Microsoft refuses
		tenantFail: options.tenantFail ?? null,
		policies: { server: new Map(), account: new Map() },
		tokens: [],
		reorderCalls: [],
		deltaCalls: [],
		compareCalls: [],
		next2: { plan: 1, compare: 1, batch: 1, token: 1 },
		// The demo runs jobs slowly enough to watch them.
		demo: options.demo === true
	};
	if (options.demo) seedDemo();
	else if (options.tenant) seedTenant();
}
reset();

function publish(kind, data) {
	for (const fn of listeners) fn(kind, data);
}

function accountView(a) {
	const job = [...state.jobs.values()].filter((j) => j.account_id === a.id).sort((x, y) => y.id - x.id)[0] ?? null;
	const { srcSecrets, tgtSecrets, ...rest } = a;
	const source = a.source ?? { kind: 'pst', config: { pst_id: a.pst_id }, has_secret: false };
	return { ...rest, source: a.source ? { ...a.source, has_secret: !!Object.keys(srcSecrets ?? {}).length || !!a.source.connection } : { ...source, config: { pst_id: a.pst_id } }, target: { ...a.target, has_secret: !!Object.keys(tgtSecrets ?? {}).length }, job };
}

/** Same rules as registry.normalize_config and merge_secrets. Returns [config, secrets] or throws a message. */
function checkEndpoint(kind, role, config, current, given) {
	const spec = CATALOGUE.find((c) => c.kind === kind);
	const def = spec?.[role];
	if (!def) throw new Error(`unknown ${role} connector '${kind}'`);
	const out = {};
	for (const name of Object.keys(config ?? {})) {
		const f = def.fields.find((x) => x.name === name);
		if (!f) throw new Error(`unknown field '${name}'`);
		if (f.secret) throw new Error(`'${name}' is a secret: send it in \`secrets\``);
	}
	for (const f of def.fields.filter((x) => !x.secret)) {
		let v = config?.[f.name];
		if (typeof v === 'string') v = v.trim();
		if (v == null || v === '') v = f.default;
		if (v == null || v === '') {
			if (f.required) throw new Error(`'${f.name}' is required`);
			continue;
		}
		out[f.name] = v;
	}
	const names = def.fields.filter((x) => x.secret).map((x) => x.name);
	let secrets = Object.fromEntries(Object.entries(current ?? {}).filter(([k]) => names.includes(k)));
	if (given !== undefined) {
		if (!Object.keys(given).length) secrets = {};
		else
			for (const [k, v] of Object.entries(given)) {
				if (!names.includes(k)) throw new Error(`unknown secret '${k}'`);
				if (v) secrets[k] = v;
				else delete secrets[k];
			}
	}
	for (const f of def.fields.filter((x) => x.secret && x.required)) if (!secrets[f.name]) throw new Error(`'${f.name}' is required`);
	return [out, secrets];
}

function log(job, level, msg) {
	const lines = state.logs.get(job.id);
	lines.push({ n: lines.length + 1, ts: Date.now() / 1000, level, msg });
}

/** The same job from the start, with its totals. */
function rewound(job) {
	const progress = emptyProgress();
	const fallback = { mail: 900, calendar: 120, contacts: 80 };
	for (const k of Object.keys(progress)) progress[k].total = job.progress[k]?.total ?? fallback[k];
	return progress;
}

function startJob(account) {
	const id = state.next.job++;
	const progress = emptyProgress();
	const totals = state.psts.get(account.pst_id)?.scan?.totals ?? { mail: 900, calendar: 120, contacts: 80 };
	for (const k of Object.keys(progress)) progress[k].total = totals[k] ?? 0;
	const job = { id, account_id: account.id, status: 'queued', phase: 'precheck', progress, current_folder: null, rate: null, rate_bytes: null, eta: null, started_at: null, finished_at: null, error: null, mode: 'full', created_at: Date.now() / 1000, tuning: null, priority: 0, batch_id: null, pause_reason: null };
	state.jobs.set(id, job);
	state.logs.set(id, []);
	log(job, 'info', 'Job queued');
	if (state.demo) pace.set(job, { tick: 1000, frac: 0.02 });
	run(job);
	return job;
}

/** Demo only: how fast a job moves, a share of its total per tick. Absent: the quick steps of the tests. */
const pace = new WeakMap();
/** Demo only: the items a finished job could not carry. */
const demoErrors = new WeakMap();

function run(job) {
	const slow = pace.get(job);
	const timer = setInterval(() => {
		if (job.status === 'paused') return;
		if (state.hold && job.status === 'queued') return;
		if (job.status === 'stopping') {
			job.status = 'stopped';
			job.finished_at = Date.now() / 1000;
			clearInterval(timer);
			publish('job', job);
			return;
		}
		if (job.status === 'queued') {
			job.status = 'running';
			job.started_at = Date.now() / 1000;
			job.tuning = { mode: 'auto', builders: 2, connections: 4, fallback: false };
		}
		const kind = ['mail', 'calendar', 'contacts'].find((k) => job.progress[k].done < job.progress[k].total);
		if (kind) {
			job.phase = kind;
			const p = job.progress[kind];
			p.done = Math.min(p.total, p.done + Math.max(1, Math.ceil(p.total * (slow?.frac ?? 0.25))));
			job.current_folder = kind === 'mail' ? 'Inbox' : kind;
			if (slow) {
				// Items per second implied by the pace, with a little life in it.
				const rate = ((p.total * slow.frac) / (slow.tick / 1000)) * (0.9 + Math.random() * 0.2);
				const left = ['mail', 'calendar', 'contacts'].reduce((n, k) => n + job.progress[k].total - job.progress[k].done, 0);
				job.rate = Math.round(rate * 10) / 10;
				job.rate_bytes = Math.round(rate * 90_000);
				job.eta = Math.round(left / rate);
			} else {
				job.rate = 42.5;
				job.rate_bytes = 1_500_000;
				job.eta = 12;
			}
			log(job, 'info', `[${kind}] ${job.current_folder}: ${p.done}/${p.total}`);
		} else {
			job.phase = 'verify';
			job.status = 'done';
			job.finished_at = Date.now() / 1000;
			job.eta = 0;
			log(job, 'info', 'Done');
			clearInterval(timer);
		}
		publish('job', job);
	}, slow?.tick ?? state.jobTick);
	timers.add(timer);
}

function planFor(account) {
	const base = { account_id: account.id, email: account.email, targets: {}, kinds: {}, folders: [], quota: null, target_limit: null, warnings: [], notes: [] };
	const pst = state.psts.get(account.pst_id);
	if (!pst || pst.status !== 'valid') return { ...base, status: 'error', source: { kind: 'pst', ok: false, msg: 'no PST linked to this mailbox' } };
	const t = pst.scan.totals;
	const kind = (items, bytes, over = {}) => ({ folders: 1, items, bytes, done: 0, to_migrate: items, to_migrate_bytes: bytes, bytes_estimated: true, filtered: { date: 3 }, oversized: 0, oversized_bytes: 0, folders_to_create: 1, excluded_folders: 0, excluded_items: 0, ...over });
	const out = { ...base, status: 'ok', source: { kind: 'pst', ok: true, msg: `${pst.filename}: 3 folder(s) analysed` }, kinds: { mail: kind(t.mail, 9_000_000), calendar: kind(t.calendar, null), contacts: kind(t.contacts, null) } };
	out.folders = [
		{ path: ['Inbox'], items: t.mail, bytes: 9_000_000, to_migrate: t.mail, target: 'INBOX', create: false, excluded: false, filtered: {}, oversized: 0 },
		{ path: ['Old mail'], items: 10, bytes: 1000, to_migrate: 10, target: 'Archive/Old', create: true, excluded: false, filtered: {}, oversized: 0 }
	];
	out.notes.push('sizes of a PST are estimated from the file size');
	if (account.email.startsWith('bob')) {
		out.status = 'warning';
		out.quota = { used: 1_000_000, limit: 5_000_000, free: 4_000_000, need: 9_000_000, enough: false };
		out.kinds.mail.oversized = 2;
		out.warnings.push('quota: 3 MB free on the target for about 8 MB to migrate');
	} else {
		out.quota = { used: 1_000_000, limit: 100_000_000, free: 99_000_000, need: 9_000_000, enough: true };
	}
	return out;
}

function planView(p, full = true) {
	const running = Date.now() - p.at < state.planDelay;
	const ids = p.account_ids;
	const accounts = running ? [] : ids.map((id) => planFor(state.accounts.get(id)));
	const t = { accounts: accounts.length, ok: 0, warning: 0, error: 0, to_migrate: 0, to_migrate_bytes: 0, oversized: 0, filtered: 0 };
	for (const a of accounts) {
		t[a.status]++;
		for (const k of Object.values(a.kinds)) {
			t.to_migrate += k.to_migrate;
			t.to_migrate_bytes += k.to_migrate_bytes ?? 0;
		}
	}
	return { id: p.id, status: running ? 'running' : 'done', created_at: p.at / 1000, updated_at: Date.now() / 1000, account_ids: ids, error: null, result: { accounts, totals: t, done: accounts.length, total: ids.length } };
}

function compareView(c) {
	const running = Date.now() - c.at < 600;
	const row = (kind, target, folders, expected, onTarget) => ({ kind, target, folders, expected, on_target: onTarget, missing: Math.max(0, expected - onTarget), extra: 0, sent: expected, errors: 0, pending: 0, filtered: 0, skipped: 0 });
	const folders = [row('mail', 'INBOX', ['Inbox'], 900, 898), row('calendar', 'Calendar', ['Calendar'], 120, 120), row('contacts', 'Contacts', ['Contacts'], 80, 80)];
	return { id: c.id, job_id: c.job_id, status: running ? 'running' : 'done', detail: false, error: null, result: running ? null : { ok: false, job_id: c.job_id, totals: { expected: 1100, on_target: 1098, missing: 2, sent: 1100, errors: 0, pending: 0, filtered: 0, skipped: 0 }, folders } };
}

function batchView(b) {
	const jobs = [...state.jobs.values()].filter((j) => j.batch_id === b.id);
	const by = {};
	const items = { mail: { done: 0, total: 0, errors: 0 }, calendar: { done: 0, total: 0, errors: 0 }, contacts: { done: 0, total: 0, errors: 0 } };
	for (const j of jobs) {
		by[j.status] = (by[j.status] ?? 0) + 1;
		for (const k of Object.keys(items)) for (const f of ['done', 'total', 'errors']) items[k][f] += j.progress[k][f];
	}
	const active = ['queued', 'running', 'paused', 'stopping'].reduce((n, s) => n + (by[s] ?? 0), 0);
	const st = !jobs.length ? 'empty' : !active ? 'finished' : by.running || by.stopping ? 'running' : by.queued ? 'queued' : 'paused';
	return { id: b.id, name: b.name, created_at: b.created_at, state: st, jobs: jobs.length, by_status: by, items, finished_at: null, failed: by.failed ?? 0, with_errors: by.done_with_errors ?? 0 };
}

function batchAct(b, action) {
	let changed = 0;
	let skipped = 0;
	for (const j of [...state.jobs.values()].filter((x) => x.batch_id === b.id)) {
		let ok = false;
		if (action === 'pause' && ['queued', 'running'].includes(j.status)) (j.status = 'paused'), (ok = true);
		else if (action === 'resume' && j.status === 'paused') (j.status = 'running'), (ok = true);
		else if (action === 'stop' && ['queued', 'running', 'paused'].includes(j.status)) (j.status = 'stopping'), (ok = true);
		else if (action === 'retry-failed' && ['failed', 'done_with_errors'].includes(j.status)) {
			j.status = 'queued';
			j.progress = rewound(j);
			run(j);
			ok = true;
		}
		if (ok) {
			changed++;
			publish('job', j);
		} else skipped++;
	}
	return { ...batchView(b), changed, skipped };
}

/** Same checks as policy.normalize, with the same reasons. Returns [policy|null] or throws a message. */
function checkPolicy(doc, server) {
	if (doc == null || !Object.keys(doc).length) return null;
	const allowed = ['folder_map', 'exclude_folders', 'date_from', 'date_to', 'max_size_mb', ...(server ? ['window', 'rate_mb_s'] : [])];
	const unknown = Object.keys(doc).filter((k) => !allowed.includes(k)).sort();
	if (unknown.length) throw new Error(`unknown or not allowed here: ${unknown.join(', ')}`);
	const out = {};
	if (doc.folder_map?.length) out.folder_map = doc.folder_map;
	if (doc.exclude_folders?.length) out.exclude_folders = doc.exclude_folders;
	for (const k of ['date_from', 'date_to']) if (doc[k]) out[k] = doc[k];
	if (out.date_from && out.date_to && out.date_from > out.date_to) throw new Error('date_from is after date_to');
	if (doc.max_size_mb != null && doc.max_size_mb !== '') {
		if (typeof doc.max_size_mb !== 'number') throw new Error('max_size_mb must be a number');
		if (doc.max_size_mb <= 0) throw new Error('max_size_mb must be positive');
		out.max_size_mb = doc.max_size_mb;
	}
	if (server && doc.window) out.window = { tz: 'UTC', ...doc.window };
	if (server && doc.rate_mb_s != null && doc.rate_mb_s !== '') {
		if (typeof doc.rate_mb_s !== 'number') throw new Error('rate_mb_s must be a number');
		if (doc.rate_mb_s <= 0) throw new Error('rate_mb_s must be positive');
		out.rate_mb_s = doc.rate_mb_s;
	}
	return Object.keys(out).length ? out : null;
}

function effectivePolicy(account) {
	const srv = state.policies.server.get(account.server_id) ?? {};
	const acc = state.policies.account.get(account.id) ?? {};
	const out = { ...srv, ...acc };
	const maps = [...(acc.folder_map ?? []), ...(srv.folder_map ?? [])];
	const ex = [...(acc.exclude_folders ?? []), ...(srv.exclude_folders ?? [])];
	if (maps.length) out.folder_map = maps;
	if (ex.length) out.exclude_folders = ex;
	if (srv.window) out.window = srv.window;
	return out;
}

/** A response: the host turns it into HTTP or into a Response. A string body is plain text, the rest is JSON. */
const send = (code, body, headers = {}) => ({ code, body, headers });
const fail = (code, detail) => send(code, { detail });

/** The controls of the tests (/__mock/*). `body` is the parsed JSON. */
export async function control(path, body) {
	if (path === '/__mock/reset') {
		reset(body);
		return send(200, { ok: true });
	}
	if (path === '/__mock/state') return send(200, { testCalls: state.testCalls, runCalls: state.runCalls, uploadOrder: state.uploadOrder, intake: state.intake, intakePuts: state.intakePuts, notifyTests: state.notifyTests, routeCalls: state.routeCalls, probeCalls: state.probeCalls, fetchCalls: state.fetchCalls, cancelCalls: state.cancelCalls, retryCalls: state.retryCalls, accounts: [...state.accounts.values()].map(accountView), runBodies: state.runBodies, reorderCalls: state.reorderCalls, deltaCalls: state.deltaCalls, compareCalls: state.compareCalls, tokens: state.tokens, linkCalls: state.linkCalls, sourceTests: state.sourceTests, provisionCalls: state.provisionCalls, policies: { server: Object.fromEntries(state.policies.server), account: Object.fromEntries(state.policies.account) } });
	if (path === '/__mock/provision') {
		// How the next "Connect with Microsoft" flows end: null, 'admin' (not an administrator), 'expired' (code expired),
		// 'propagation' (saved, permissions not active yet) or 'lost' (404 from the second poll, as after a restart).
		state.provisionFail = body.fail ?? null;
		return send(200, { ok: true });
	}
	if (path === '/__mock/inbox') {
		state.inbox = body.files ?? [];
		return send(200, { ok: true });
	}
	if (path === '/__mock/pst') {
		// Insert or update a PST row, and tell the stream: what the worker does while fetching.
		const current = state.psts.get(body.pst.id) ?? {};
		const pst = { ...validPst(body.pst.id, body.pst.filename ?? current.filename ?? 'x.pst', body.pst.size ?? 0), ...current, ...body.pst };
		state.psts.set(pst.id, pst);
		publish('pst', pst);
		return send(200, pst);
	}
	if (path === '/__mock/job') {
		// Change a job and tell the stream: a pause by the allowed hours, tasks in the progress, ...
		const job = state.jobs.get(body.id);
		Object.assign(job, body.patch);
		publish('job', job);
		return send(200, job);
	}
	if (path === '/__mock/event') {
		publish(body.kind, body.data);
		return send(200, { ok: true });
	}
	if (path === '/__mock/burst') {
		// Progress events for every account with a job, several rounds, as fast as the socket takes them.
		const rounds = body.rounds ?? 50;
		for (let round = 0; round < rounds; round++) {
			for (const account of state.accounts.values()) {
				const percent = (round * 2) % 100;
				const progress = emptyProgress();
				progress.mail = { done: percent * 9, total: 900, errors: 0 };
				publish('job', { id: 100000 + account.id, account_id: account.id, status: 'running', phase: 'mail', progress, current_folder: 'Inbox', rate: 40, rate_bytes: null, eta: 30, started_at: Date.now() / 1000, finished_at: null, error: null, tuning: null });
			}
			await new Promise((r) => setTimeout(r, body.gap ?? 10));
		}
		return send(200, { ok: true });
	}
	return fail(404, 'unknown control');
}

/**
 * One request of /api, without the sign-in and the event stream (the host does those).
 * req: { method, path ('/servers', no /api), query (URLSearchParams), headers (lower case), text(), bytes() }.
 */
export async function handle(req) {
	const { path } = req;
	const url = { searchParams: req.query };
	const m = req.method;
	const parts = path.split('/').filter(Boolean);
	if (path === '/health') return send(200, { ok: true, worker: { alive: true, last_seen: Date.now() / 1000 }, time: Date.now() / 1000, version: hooks.version });
	if (path === '/version') return send(200, { version: hooks.version });
	if (path === '/me') return send(200, { authenticated: true, user: 'admin' });
	// The body of a chunk can be large: it is only read when something checks it.
	const isChunk = parts[0] === 'uploads' && parts[2] === 'chunks';
	const raw = isChunk ? '' : await req.text();
	const json = () => JSON.parse(raw || '{}');

	if (parts[0] === 'servers') {
		if (parts.length === 1) {
			if (m === 'GET') return send(200, [...state.servers.values()]);
			const id = state.next.server++;
			const server = { ...makeServer(id, '', ''), ...json(), id };
			state.servers.set(id, server);
			return send(201, server);
		}
		const server = state.servers.get(Number(parts[1]));
		if (!server) return fail(404, 'unknown server');
		if (parts[2] === 'policy') {
			if (m === 'PUT') {
				try {
					state.policies.server.set(server.id, checkPolicy(json(), true));
				} catch (error) {
					return fail(422, error.message);
				}
			}
			return send(200, { policy: state.policies.server.get(server.id) ?? null });
		}
		if (parts[2] === 'test') {
			// Stored like the real server does: the list shows the verdict after a reload.
			const result = { imap: { ok: true, msg: `IMAP server reachable (${server.imap_host}:${server.imap_port})`, capabilities: [] }, caldav: server.dav_enabled ? { ok: true, msg: 'reachable (HTTP 207)' } : { ok: true, found: server.imap_host.includes('dav'), msg: server.imap_host.includes('dav') ? 'DAV is off, but https://' + server.imap_host + '/.well-known/caldav answers (HTTP 401)' : 'DAV disabled for this server' }, carddav: server.dav_enabled ? { ok: false, msg: 'https://dav.example.com/ unreachable: timeout' } : { ok: true, found: false, msg: 'DAV disabled for this server' } };
			server.last_test = Date.now() / 1000;
			server.last_test_result = result;
			return send(200, result);
		}
		if (m === 'GET') return send(200, server);
		if (m === 'PUT') {
			const before = server.dav_enabled;
			Object.assign(server, json(), { id: server.id });
			if (before !== server.dav_enabled) Object.assign(server, { last_test: null, last_test_result: null });
			return send(200, server);
		}
		if (m === 'DELETE') {
			state.servers.delete(server.id);
			for (const a of [...state.accounts.values()]) if (a.server_id === server.id) state.accounts.delete(a.id);
			return send(200, { ok: true });
		}
	}

	if (parts[0] === 'repair') {
		const r = handleRepair(parts.slice(1), m, json);
		if (r) return r;
	}

	if (parts[0] === 'source-connections') {
		const r = handleTenants(parts.slice(1), m, json, url.searchParams);
		if (r) return r;
	}

	// The tenant connector is only offered once a tenant is seeded: other specs list the source kinds.
	if (path === '/connectors' && m === 'GET') return send(200, state.tenantOn ? CATALOGUE.map((c) => (c.kind === 'm365_graph' ? { ...c, provision: state.provisionOn } : c)) : CATALOGUE.filter((c) => c.kind !== 'm365_graph'));
	if (parts[0] === 'connectors' && parts[2] === 'test' && m === 'POST') {
		const body = json();
		const spec = CATALOGUE.find((c) => c.kind === parts[1]);
		if (!spec) return fail(404, 'unknown connector');
		const account = body.account_id != null ? state.accounts.get(body.account_id) : null;
		const key = body.role === 'source' ? 'srcSecrets' : 'tgtSecrets';
		const current = account && account[body.role]?.kind === parts[1] ? account[key] : {};
		state.probeCalls.push({ kind: parts[1], body });
		try {
			checkEndpoint(parts[1], body.role ?? 'source', body.config, current, body.secrets);
		} catch (error) {
			return fail(422, error.message);
		}
		const host = String(body.config?.host ?? body.config?.server ?? body.config?.mailbox ?? '');
		if (host.startsWith('bad')) return send(200, { ok: false, error_kind: 'unreachable', msg: `cannot connect to ${host}` });
		if (host.startsWith('wrong')) return send(200, { ok: false, error_kind: 'auth', msg: 'LOGIN failed for jane@example.com' });
		return body.role === 'target' ? send(200, { ok: true, msg: 'connection successful', quota: null }) : send(200, { ok: true, msg: '3 folder(s) found', folders: [{ path: 'INBOX', kind: 'mail', special: 'inbox', count: 12 }] });
	}

	if (path === '/intake/settings') {
		if (m === 'GET') return send(200, state.intake);
		const patch = json();
		state.intakePuts.push(patch);
		const next = structuredClone(state.intake);
		const n = patch.notifications;
		if (n) {
			for (const key of ['enabled', 'mask_addresses', 'group_window', 'disk_low_mb']) if (key in n) next.notifications[key] = n[key];
			Object.assign(next.notifications.events, n.events ?? {});
			for (const [ch, secret] of [['ntfy', 'token'], ['webhook', 'token'], ['email', 'password']]) {
				const c = n[ch];
				if (!c) continue;
				const { [secret]: value, ...plain } = c;
				Object.assign(next.notifications[ch], plain);
				if (value !== undefined) next.notifications[ch][`has_${secret}`] = value !== '';
			}
			const d = next.notifications;
			if (d.enabled && d.ntfy.enabled && !d.ntfy.topic) return fail(422, 'ntfy.topic is required when ntfy is enabled');
			if (d.enabled && d.webhook.enabled && !d.webhook.url) return fail(422, 'webhook.url is required when the webhook is enabled');
		}
		if (patch.inbox) Object.assign(next.inbox, patch.inbox);
		if (patch.auto_chain) {
			for (const dom of Object.values(patch.auto_chain.default_domains ?? {})) if (!/^[a-z0-9.-]+$/i.test(dom)) return fail(422, `invalid domain: ${dom}`);
			Object.assign(next.auto_chain, patch.auto_chain);
		}
		state.intake = next;
		return send(200, next);
	}
	if (path === '/intake/notifications/test' && m === 'POST') {
		const body = json();
		const cfg = state.intake.notifications;
		const names = body.channel ? [body.channel] : ['ntfy', 'webhook', 'email'].filter((c) => cfg[c].enabled);
		if (!names.length) return fail(422, 'no channel enabled');
		state.notifyTests++;
		const results = Object.fromEntries(names.map((c) => [c, c === 'webhook' && cfg.webhook.url.includes('fail') ? { ok: false, error: 'ConnectError: connection refused' } : { ok: true, error: null }]));
		return send(200, { ok: Object.values(results).every((x) => x.ok), results });
	}
	if (path === '/intake/inbox') return send(200, { path: state.intake.inbox.path, enabled: state.intake.inbox.enabled, settle_seconds: state.intake.inbox.settle_seconds, files: state.inbox });
	if (path === '/psts/fetch' && m === 'POST') {
		const body = json();
		const need = { http: ['url'], sftp: ['host', 'path', 'username'], s3: ['endpoint', 'bucket', 'key', 'access_key', 'secret_key'] }[body.kind];
		if (!need) return fail(422, 'kind must be http, sftp or s3');
		for (const f of need) if (!body[f]) return fail(422, `${f} is required`);
		if (String(body.url ?? '').includes('169.254')) return fail(422, 'destination refused: link-local address');
		state.fetchCalls.push(body);
		const id = state.next.pst++;
		const origin = body.url ? new URL(body.url).host : (body.host ?? body.endpoint);
		const pst = { id, filename: body.filename || String(body.url ?? body.path ?? body.key).split('/').pop(), size: 0, status: 'fetching', scan: null, error: null, pst_sha: null, created_at: Date.now() / 1000, updated_at: Date.now() / 1000, source: 'fetch', origin, received_bytes: 0, chain: null };
		state.psts.set(id, pst);
		publish('pst', pst);
		return send(201, pst);
	}

	if (parts[0] === 'accounts') {
		if (parts[1] === 'csv-template') return send(200, 'email;password;login\nalice@example.com;password;\n', { 'Content-Type': 'text/csv' });
		if (parts[1] === 'import-csv') return send(200, { created: 1, updated: 0, errors: [{ line: 3, msg: 'invalid email address' }], warnings: [{ line: 4, msg: "PST 'missing.pst' is not on the server: not linked" }] });
		if (parts[1] === 'export-csv') return send(200, 'email;login;server;pst;mail;calendar;contacts;source_kind\r\nalice@example.com;;Production;alice.pst;1;1;1;\r\n', { 'Content-Type': 'text/csv; charset=utf-8' });
		if (parts.length === 1) {
			if (m === 'GET') {
				const server = url.searchParams.get('server_id');
				return send(200, [...state.accounts.values()].filter((a) => !server || a.server_id === Number(server)).sort((a, b) => a.email.localeCompare(b.email)).map(accountView));
			}
			const body = json();
			if ([...state.accounts.values()].some((a) => a.server_id === body.server_id && a.email === body.email)) return fail(409, 'this mailbox already exists on this server');
			const id = state.next.account++;
			const account = makeAccount(id, body.email, body.server_id, { login: body.login || body.email, has_password: !!body.password });
			state.accounts.set(id, account);
			publish('account', accountView(account));
			return send(201, accountView(account));
		}
		const account = state.accounts.get(Number(parts[1]));
		if (!account) return fail(404, 'unknown mailbox');
		if (parts[2] === 'test') {
			state.testCalls[account.id] = (state.testCalls[account.id] ?? 0) + 1;
			await new Promise((r) => setTimeout(r, state.testDelay));
			const ok = !account.email.startsWith('bob');
			const result = { imap: { ok: true, msg: 'IMAP connection successful', quota: null }, caldav: { ok, msg: ok ? 'CalDAV reachable' : 'HTTP 401 from dav.example.com' }, carddav: { ok: true, msg: 'CardDAV reachable' } };
			account.status = ok ? 'ok' : 'error';
			account.last_test = Date.now() / 1000;
			account.last_test_result = result;
			publish('account', accountView(account));
			return send(200, result);
		}
		if (parts[2] === 'policy') {
			if (m === 'PUT') {
				try {
					state.policies.account.set(account.id, checkPolicy(json(), false));
				} catch (error) {
					return fail(422, error.message);
				}
			}
			return send(200, { policy: state.policies.account.get(account.id) ?? null, effective: effectivePolicy(account) });
		}
		if (parts[2] === 'source' || parts[2] === 'target') {
			const role = parts[2];
			if (m === 'GET') return send(200, accountView(account)[role]);
			if ([...state.jobs.values()].some((j) => j.account_id === account.id && ['queued', 'running', 'paused', 'stopping'].includes(j.status))) return fail(409, 'a job is active for this mailbox');
			const body = json();
			state.routeCalls.push({ id: account.id, role, body });
			const key = role === 'source' ? 'srcSecrets' : 'tgtSecrets';
			if (role === 'source' && (body.kind ?? 'pst') === 'pst') {
				account.source = undefined;
				account[key] = {};
			} else if (role === 'target' && !body.kind) {
				account.target = { kind: null, config: {}, has_secret: false };
				account[key] = {};
			} else {
				if (role === 'source' && body.connection_id != null) {
					const conn = state.sourceConnections.get(Number(body.connection_id));
					if (!conn) return fail(404, 'unknown source connection');
					account.source = { kind: body.kind, config: { mailbox: body.config?.mailbox, include_tasks: body.config?.include_tasks ?? true }, has_secret: false, connection: { id: conn.id, name: conn.name } };
					account.srcSecrets = {};
					publish('account', accountView(account));
					return send(200, accountView(account));
				}
				try {
					const same = account[role]?.kind === body.kind;
					const [config, secrets] = checkEndpoint(body.kind, role, body.config, same ? account[key] : {}, body.secrets);
					account[role] = { kind: body.kind, config, has_secret: false };
					account[key] = secrets;
				} catch (error) {
					return fail(422, error.message);
				}
			}
			publish('account', accountView(account));
			return send(200, accountView(account));
		}
		if (parts[2] === 'pst') {
			account.pst_id = json().pst_id ?? null;
			publish('account', accountView(account));
			return send(200, accountView(account));
		}
		if (m === 'GET') return send(200, accountView(account));
		if (m === 'PUT') {
			const body = json();
			if (body.options) account.options = { ...account.options, ...body.options };
			if (body.password) account.status = 'new';
			if (body.login !== undefined) account.login = body.login || account.email;
			publish('account', accountView(account));
			return send(200, accountView(account));
		}
		if (m === 'DELETE') {
			state.accounts.delete(account.id);
			return send(200, { ok: true });
		}
	}

	if (parts[0] === 'uploads') {
		if (parts.length === 1 && m === 'POST') {
			const body = json();
			const key = `${body.filename}|${body.size}|${body.last_modified}`;
			let upload = [...state.uploads.values()].find((u) => u.key === key);
			if (!upload) {
				const id = state.next.pst++;
				upload = { id, key, filename: body.filename, size: body.size, chunk_size: CHUNK, received: new Set(), status: 'uploading' };
				state.uploads.set(id, upload);
				state.psts.set(id, { id, filename: body.filename, size: body.size, status: 'uploading', scan: null, error: null, pst_sha: null, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 });
				state.uploadOrder.push(body.filename);
			}
			return send(200, { id: upload.id, chunk_size: CHUNK, received: [...upload.received] });
		}
		const upload = state.uploads.get(Number(parts[1]));
		if (!upload) return fail(404, 'unknown upload');
		if (parts.length === 2) return send(200, { id: upload.id, filename: upload.filename, size: upload.size, chunk_size: CHUNK, received: [...upload.received], status: upload.status, chunks: Math.max(1, Math.ceil(upload.size / CHUNK)), error: null });
		if (parts[2] === 'chunks') {
			if (state.chunkDelay) await new Promise((r) => setTimeout(r, state.chunkDelay));
			if (hooks.chunkOk && !(await hooks.chunkOk(await req.bytes(), req.headers['x-chunk-sha256']))) return fail(422, 'incorrect SHA-256');
			upload.received.add(Number(parts[3]));
			return send(200, { ok: true, received: [...upload.received] });
		}
		if (parts[2] === 'complete') {
			upload.status = 'validating';
			const pst = state.psts.get(upload.id);
			pst.status = 'validating';
			publish('pst', pst);
			setTimeout(() => {
				Object.assign(pst, validPst(pst.id, pst.filename, pst.size));
				upload.status = 'valid';
				publish('pst', pst);
			}, 200);
			return send(200, { id: upload.id, status: 'validating', pst_id: upload.id, pst });
		}
	}

	if (parts[0] === 'psts') {
		if (parts.length === 1) return send(200, [...state.psts.values()]);
		const pst = state.psts.get(Number(parts[1]));
		if (!pst) return fail(404, 'unknown PST');
		if (parts[2] === 'cancel' && m === 'POST') {
			state.cancelCalls.push(pst.id);
			if (pst.status !== 'fetching') return fail(409, 'this PST is not being fetched');
			if (pst.slowCancel) {
				pst.status = 'cancelling';
				publish('pst', pst);
				return send(200, { ok: true, state: 'cancelling' });
			}
			state.psts.delete(pst.id);
			publish('pst', { id: pst.id, deleted: true });
			return send(200, { ok: true, state: 'removed' });
		}
		if (parts[2] === 'retry-fetch' && m === 'POST') {
			state.retryCalls.push(pst.id);
			if (pst.status !== 'fetch_failed') return fail(409, 'only a failed fetch can be retried');
			Object.assign(pst, { status: 'fetching', error: null });
			publish('pst', pst);
			return send(200, pst);
		}
		if (m === 'DELETE') {
			state.psts.delete(pst.id);
			state.uploads.delete(pst.id);
			return send(200, { ok: true });
		}
		return send(200, pst);
	}

	if (path === '/runs/plan' && m === 'POST') {
		const ids = json().account_ids ?? [];
		const unknown = ids.filter((id) => !state.accounts.has(id));
		if (unknown.length) return fail(422, { msg: 'unknown mailbox(es)', account_ids: unknown });
		const plan = { id: state.next2.plan++, account_ids: ids, at: Date.now() };
		state.plans.set(plan.id, plan);
		return send(202, { id: plan.id, status: 'queued', account_ids: ids, error: null, result: null });
	}
	if (parts[0] === 'plans') {
		const plan = state.plans.get(Number(parts[1]));
		if (!plan) return fail(404, 'unknown plan');
		return send(200, planView(plan));
	}
	if (path === '/runs' && m === 'POST') {
		const body = json();
		const ids = body.account_ids ?? [];
		state.runCalls.push(ids);
		state.runBodies.push(body);
		let batch = null;
		if (body.name) {
			batch = { id: state.next2.batch++, name: body.name, created_at: Date.now() / 1000 };
			state.batches.set(batch.id, batch);
		}
		const jobs = ids.map((id) => state.accounts.get(id)).filter(Boolean).map(startJob);
		for (const job of jobs) {
			if (batch) job.batch_id = batch.id;
			if (body.mode) job.mode = body.mode;
			publish('job', job);
		}
		return send(200, jobs);
	}
	if (path === '/batches' && m === 'GET') return send(200, [...state.batches.values()].reverse().map(batchView));
	if (parts[0] === 'batches') {
		const batch = state.batches.get(Number(parts[1]));
		if (!batch) return fail(404, 'unknown batch');
		if (parts[2]) return send(200, batchAct(batch, parts[2]));
		return send(200, batchView(batch));
	}
	if (path === '/tokens' && m === 'GET') return send(200, [...state.tokens].reverse().map(({ secret, ...view }) => view));
	if (path === '/tokens' && m === 'POST') {
		const token = { id: state.next2.token++, name: json().name || 'token', prefix: `mccb_${String(state.next2.token).padStart(8, '0')}`, created_at: Date.now() / 1000, last_used_at: null, revoked_at: null };
		state.tokens.push(token);
		return send(201, { ...token, token: `${token.prefix}_${'x'.repeat(43)}` });
	}
	if (parts[0] === 'tokens' && m === 'DELETE') {
		const token = state.tokens.find((t) => t.id === Number(parts[1]));
		if (!token) return fail(404, 'unknown token');
		token.revoked_at ??= Date.now() / 1000;
		return send(200, token);
	}

	if (parts[0] === 'jobs') {
		if (parts.length === 1) return send(200, [...state.jobs.values()]);
		if (parts[1] === 'reorder' && m === 'POST') {
			const ids = json().job_ids ?? [];
			state.reorderCalls.push(ids);
			const queued = [...state.jobs.values()].filter((j) => j.status === 'queued').sort((a, b) => b.priority - a.priority || a.id - b.id).map((j) => j.id);
			const order = [...ids.filter((id) => queued.includes(id)), ...queued.filter((id) => !ids.includes(id))];
			order.forEach((id, i) => {
				const j = state.jobs.get(id);
				j.priority = order.length - i;
				publish('job', j);
			});
			return send(200, { queue: order });
		}
		const job = state.jobs.get(Number(parts[1]));
		if (!job) return fail(404, 'unknown job');
		if (parts.length === 2) return send(200, job);
		const action = parts[2];
		if (action === 'compare') {
			if (parts[3] === 'csv') {
				const c = [...state.compares.values()].filter((x) => x.job_id === job.id).pop();
				if (!c) return fail(404, 'no finished comparison for this job');
				return send(200, 'kind;source_folders;target_folder;expected;on_target;missing\r\nmail;Inbox;INBOX;900;898;2\r\n', { 'Content-Type': 'text/csv; charset=utf-8' });
			}
			if (m === 'POST' || url.searchParams.get('run')) {
				state.compareCalls.push(job.id);
				const c = { id: state.next2.compare++, job_id: job.id, at: Date.now() };
				state.compares.set(c.id, c);
				return send(202, { id: c.id, job_id: job.id, status: 'queued', result: null });
			}
			const c = [...state.compares.values()].filter((x) => x.job_id === job.id).pop();
			return send(200, c ? compareView(c) : { status: 'none', job_id: job.id, result: null });
		}
		if (action === 'filtered') return send(200, [{ id: 11, kind: 'mail', folder: 'Inbox', item_key: 'k11', subject: 'Old newsletter', size: 4096, error: 'filtered: date', updated_at: Date.now() / 1000, reason: 'date' }]);
		if (action === 'oversized') return send(200, [{ id: 12, kind: 'mail', folder: 'Sent', item_key: 'k12', subject: 'Holiday video', size: 52_428_800, error: '[oversize] 50 MB', updated_at: Date.now() / 1000 }]);
		if (action === 'priority' && m === 'PUT') job.priority = json().priority;
		else if (action === 'delta') {
			state.deltaCalls.push(job.id);
			job.status = 'queued';
			job.mode = 'delta';
			job.finished_at = null;
			job.progress = rewound(job);
			run(job);
		} else if (action === 'pause') job.status = 'paused';
		else if (action === 'resume') job.status = 'running';
		else if (action === 'stop') job.status = 'stopping';
		else if (action === 'retry' || action === 'retry-errors') {
			job.status = 'queued';
			job.finished_at = null;
			job.progress = rewound(job);
			run(job);
		} else if (action === 'log') {
			const after = Number(url.searchParams.get('after') ?? 0);
			const lines = state.logs.get(job.id).filter((l) => l.n > after);
			return send(200, { lines, next: lines.at(-1)?.n ?? after });
		} else if (action === 'errors') {
			return send(200, demoErrors.get(job) ?? [{ id: 1, kind: 'mail', folder: 'Inbox', item_key: 'k1', subject: 'Quarterly report', error: 'APPEND refused: message too large', updated_at: Date.now() / 1000 }]);
		} else if (action === 'report') {
			if (!['done', 'done_with_errors', 'stopped', 'failed'].includes(job.status)) return fail(404, 'report not available yet');
			const part = (k) => ({ sent: job.progress[k].done, present: job.progress[k].done, errors: job.progress[k].errors });
			return send(200, { job_id: job.id, mail: part('mail'), calendar: part('calendar'), contacts: part('contacts'), sample_check: '20/20 mails read back', filtered: { total: 1, reasons: { date: 1 } }, oversized: { count: 1, bytes: 52_428_800 } });
		}
		publish('job', job);
		return send(200, job);
	}

	return fail(404, 'unknown API route');
}

/**
 * The state of the static demo: ten fictional mailboxes (example.com, documentation names), a batch with
 * jobs moving at different paces, one that needs attention, some done, files of several kinds.
 */
function seedDemo() {
	const now = Date.now() / 1000;
	const ago = (minutes) => now - minutes * 60;
	const GB = 1_000_000_000;
	for (const t of timers) clearInterval(t);
	timers = new Set();

	state.servers = new Map([
		makeServer(1, 'Main mail server', 'imap.example.com', { caldav_url: 'https://dav.example.com/', carddav_url: 'https://dav.example.com/', workers: 4 }),
		makeServer(2, 'Archive server', 'archive.home.arpa', { imap_port: 143, imap_security: 'starttls', dav_enabled: false, workers: 2, root_folder: 'Imported' })
	].map((x) => [x.id, x]));

	const pst = (id, filename, size, totals, over = {}) => {
		const folders = [
			{ path: ['Inbox'], kind: 'mail', count: Math.round(totals.mail * 0.62) },
			{ path: ['Sent Items'], kind: 'mail', count: Math.round(totals.mail * 0.23) },
			{ path: ['Archive', '2023'], kind: 'mail', count: totals.mail - Math.round(totals.mail * 0.62) - Math.round(totals.mail * 0.23) },
			{ path: ['Calendar'], kind: 'calendar', count: totals.calendar },
			{ path: ['Contacts'], kind: 'contacts', count: totals.contacts }
		];
		return { ...validPst(id, filename, Math.round(size)), created_at: ago(600 + id * 7), updated_at: ago(590 + id * 7), pst_sha: String(id).repeat(64).slice(0, 64), source: 'upload', scan: { folders, totals, errors: [] }, ...over };
	};
	const totals = (mail, calendar, contacts) => ({ mail, calendar, contacts });
	const psts = [
		pst(1, 'amelie-fontaine.pst', 4.3 * GB, totals(48210, 1840, 612)),
		pst(2, 'tomas-novak.pst', 2.1 * GB, totals(21940, 760, 340)),
		pst(3, 'priya-raman.pst', 7.8 * GB, totals(83100, 2210, 905)),
		pst(4, 'lucas-meyer.pst', 3.4 * GB, totals(36420, 1190, 431)),
		pst(5, 'ines-costa.pst', 1.2 * GB, totals(12880, 410, 188)),
		pst(6, 'jonas-lindqvist.pst', 0.9 * GB, totals(9640, 305, 120)),
		pst(7, 'mei-tanaka.pst', 2.6 * GB, totals(27350, 980, 402)),
		pst(8, 'hana-kovacs.pst', 1.8 * GB, totals(19420, 650, 270)),
		pst(9, 'old-archive-2014.pst', 0.64 * GB, totals(0, 0, 0), { status: 'invalid', error: 'Not a PST file: the !BDN signature is missing.', scan: null, pst_sha: null }),
		pst(10, 'shared-mailboxes.zip', 0.35 * GB, totals(6200, 140, 85), {
			scan: { format: 'files', contents: ['mbox', 'ics', 'vcf'], folders: [{ path: ['support.mbox'], kind: 'mail', count: 6200 }, { path: ['team'], kind: 'calendar', count: 140 }, { path: ['contacts'], kind: 'contacts', count: 85 }], totals: totals(6200, 140, 85), errors: [] }
		})
	];
	state.psts = new Map(psts.map((x) => [x.id, x]));

	const names = ['amelie.fontaine', 'tomas.novak', 'priya.raman', 'lucas.meyer', 'ines.costa', 'jonas.lindqvist', 'mei.tanaka', 'hana.kovacs', 'omar.haddad', 'sofia.petrova'];
	const tested = { imap: { ok: true, msg: 'IMAP connection successful', quota: null }, caldav: { ok: true, msg: 'CalDAV reachable' }, carddav: { ok: true, msg: 'CardDAV reachable' } };
	state.accounts = new Map(
		names.map((n, i) => {
			const id = i + 1;
			const over = { pst_id: id <= 8 ? id : null };
			if (id <= 8) Object.assign(over, { status: 'ok', last_test: ago(400), last_test_result: tested });
			if (id === 10) Object.assign(over, { status: 'auth_failed', last_test: ago(30), last_test_result: { imap: { ok: false, msg: 'LOGIN failed for sofia.petrova@example.com' } } });
			return [id, makeAccount(id, `${n}@example.com`, id >= 8 ? 2 : 1, over)];
		})
	);

	state.jobs = new Map();
	state.logs = new Map();
	state.batches = new Map([[1, { id: 1, name: 'Sales team, first wave', created_at: ago(380) }]]);
	const tuning = { mode: 'auto', builders: 4, connections: 8, fallback: false };
	const job = (id, accountId, status, done, over = {}) => {
		const t = state.psts.get(accountId).scan.totals;
		const progress = emptyProgress();
		for (const k of Object.keys(progress)) progress[k] = { done: Math.round(t[k] * (done[k] ?? 0)), total: t[k], errors: over.errors?.[k] ?? 0 };
		const j = { id, account_id: accountId, status, phase: over.phase ?? 'verify', progress, current_folder: null, rate: null, rate_bytes: null, eta: null, started_at: ago(over.started ?? 300), finished_at: null, error: null, mode: 'full', created_at: ago((over.started ?? 300) + 5), tuning, priority: 0, batch_id: 1, pause_reason: null };
		state.jobs.set(id, j);
		state.logs.set(id, []);
		const lines = [['info', 'Job queued'], ['info', 'Precheck: IMAP, CalDAV and CardDAV reachable, quota sufficient']];
		if (status !== 'queued') lines.push(['info', `[${j.phase}] ${j.progress.mail.done}/${j.progress.mail.total}`]);
		lines.forEach(([level, msg], n) => state.logs.get(id).push({ n: n + 1, ts: ago((over.started ?? 300) - n), level, msg }));
		state.next.job = Math.max(state.next.job, id + 1);
		return j;
	};
	const moving = (j, kind, tick, frac, rate) => {
		j.phase = kind;
		j.current_folder = kind === 'mail' ? 'Inbox' : kind === 'calendar' ? 'Calendar' : 'Contacts';
		j.rate = rate;
		j.rate_bytes = Math.round(rate * 90_000);
		j.eta = Math.round((j.progress[kind].total - j.progress[kind].done) / rate);
		pace.set(j, { tick, frac });
		run(j);
	};
	const finished = (j, minutes, rate) => {
		j.finished_at = ago(minutes);
		j.rate = rate;
		j.rate_bytes = Math.round(rate * 90_000);
		j.eta = 0;
	};
	const all = { mail: 1, calendar: 1, contacts: 1 };

	moving(job(1, 1, 'running', { mail: 0.62 }, { phase: 'mail', started: 180 }), 'mail', 1500, 0.0012, 38);
	moving(job(2, 2, 'running', { mail: 1, calendar: 0.41 }, { phase: 'calendar', started: 95 }), 'calendar', 1500, 0.01, 5.3);
	moving(job(3, 3, 'running', { mail: 0.18 }, { phase: 'mail', started: 40 }), 'mail', 1500, 0.0006, 33);
	const flagged = job(4, 4, 'done_with_errors', all, { started: 260, errors: { mail: 14, calendar: 1 } });
	finished(flagged, 70, 41);
	demoErrors.set(flagged, [
		{ id: 1, kind: 'mail', folder: 'Sent Items', item_key: 'k4-1', subject: 'Quarterly forecast v3 (final)', error: 'APPEND refused: message exceeds the size limit of the target (25 MB)', updated_at: ago(90) },
		{ id: 2, kind: 'mail', folder: 'Archive/2023', item_key: 'k4-2', subject: 'Site photos, June', error: 'APPEND refused: message exceeds the size limit of the target (25 MB)', updated_at: ago(88) },
		{ id: 3, kind: 'calendar', folder: 'Calendar', item_key: 'k4-3', subject: 'Weekly sync', error: 'Invalid recurrence rule: the series was skipped', updated_at: ago(75) }
	]);
	finished(job(5, 5, 'done', all, { started: 330 }), 190, 44);
	finished(job(6, 6, 'done', all, { started: 340 }), 250, 47);
	finished(job(7, 7, 'done', all, { started: 120 }), 20, 39);
	const waiting = job(8, 8, 'queued', {}, { phase: 'precheck', started: 2 });
	waiting.started_at = null;
	pace.set(waiting, { tick: 1000, frac: 0.002 });
	timers.add(setTimeout(() => run(waiting), 45_000));

	seedTenant();
	state.next.pst = 11;
	state.next2.batch = 2;
}

// ---------------------------------------------------------------- repair tab (fictional: a run ends at once)
const REPAIR_REPORT = {
	issues: { replacement_chars: 12, attachment_names: 4 },
	folders: { INBOX: { seen: 240, affected: 9, repaired: 0, issues: { replacement_chars: 7, attachment_names: 2 } }, 'Archive/2023': { seen: 180, affected: 5, repaired: 0, issues: { replacement_chars: 5, attachment_names: 2 } } },
	examples: [{ folder: 'INBOX', uid: '17', subject: 'Offre r\ufffdvis\ufffde', date: 'Tue, 05 Mar 2024 10:00:00 +0100', issues: ['replacement_chars'], fixed: null }],
	errors: [],
	notes: []
};

function repairRun(box, mode) {
	const id = state.repair.next.run++;
	const seen = 420;
	const affected = box.email.startsWith('clean') ? 0 : 14;
	const report = mode === 'scan' ? (affected ? REPAIR_REPORT : { issues: {}, folders: {}, examples: [], errors: [], notes: [] }) : { issues: {}, folders: {}, examples: [], errors: [], notes: [] };
	const now = Date.now() / 1000;
	const run = { id, box_id: box.id, mode, status: 'done', folder: null, error: null, created_at: now, started_at: now, finished_at: now,
		progress: { total: seen, seen, affected: mode === 'scan' ? affected : 0, repaired: mode === 'repair' ? affected : 0, restored: mode === 'restore' ? affected : 0, skipped: 0, errors: 0, folder: null, folders_done: 2, folders_total: 2 },
		issues: report.issues, report };
	state.repair.runs.set(id, run);
	return run;
}

function repairBoxView(box) {
	const runs = [...state.repair.runs.values()].filter((r) => r.box_id === box.id);
	const last = runs.length ? { ...runs[runs.length - 1], report: undefined } : null;
	return { ...box, last_scan: last, active_run: null };
}

function handleRepair(parts, m, json) {
	const R = state.repair;
	if (parts[0] === 'boxes' && parts.length === 1) {
		if (m === 'GET') return send(200, [...R.boxes.values()].map(repairBoxView));
		const body = json();
		if ([...R.boxes.values()].some((b) => b.server_id === body.server_id && b.email === body.email)) return fail(409, 'this mailbox is already in the list for this server');
		const id = R.next.box++;
		const box = { id, server_id: Number(body.server_id), email: body.email, login: body.login ?? '', has_password: true, status: 'new', last_test: null, last_test_result: null, created_at: Date.now() / 1000 };
		R.boxes.set(id, box);
		return send(201, repairBoxView(box));
	}
	if (parts[0] === 'boxes' && parts[1] === 'import-csv') return send(200, { created: 0, updated: 0, errors: [], ids: [] });
	if (parts[0] === 'boxes') {
		const box = R.boxes.get(Number(parts[1]));
		if (!box) return fail(404, 'unknown mailbox');
		if (parts[2] === 'test') {
			const ok = !box.email.startsWith('bad');
			box.status = ok ? 'ok' : 'auth_failed';
			box.last_test = Date.now() / 1000;
			box.last_test_result = { imap: { ok, msg: ok ? 'IMAP connection successful' : 'LOGIN failed', quota: null } };
			return send(200, box.last_test_result);
		}
		if (parts[2] === 'preview') {
			return send(200, { issues: ['replacement_chars'], already: false, fixes: ['replacement_chars'], left: [], error: null,
				before: { subject: 'Offre r\ufffdvis\ufffde', text: 'Bonjour, voici l\u2019offre r\ufffdvis\ufffde.', parts: [] },
				after: { subject: 'Offre r\ufffdvis\ufffde', text: 'Bonjour, voici l\u2019offre r\u00e9vis\u00e9e.', parts: [] } });
		}
		if (parts[2] === 'runs') return send(200, [...R.runs.values()].filter((r) => r.box_id === box.id).reverse());
		if (m === 'PUT') {
			Object.assign(box, json(), { id: box.id, status: 'new' });
			return send(200, repairBoxView(box));
		}
		if (m === 'DELETE') {
			R.boxes.delete(box.id);
			return send(200, { ok: true });
		}
	}
	if (parts[0] === 'runs' && parts.length === 1 && m === 'POST') {
		const body = json();
		const runs = body.box_ids.map((id) => R.boxes.get(id)).filter(Boolean).map((b) => repairRun(b, body.mode));
		return send(202, { runs: runs.map((r) => ({ ...r, report: undefined })), skipped_busy: [] });
	}
	if (parts[0] === 'runs' && parts[1]) {
		const run = R.runs.get(Number(parts[1]));
		if (!run) return fail(404, 'unknown run');
		return send(200, run);
	}
	return null;
}

// ---------------------------------------------------------------- Microsoft 365 tenants (fictional: "Contoso", no network, no secret kept)
/** The fictional directory behind every tenant connection of the demo. */
function tenantDirectory() {
	const u = (n, first, last, over = {}) => {
		const slug = `${first}.${last}`.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
		const mail = `${slug}@contoso.example`;
		return { id: `3f2a${String(n).padStart(4, '0')}-0c1d-4e5f-8a9b-${String(n).padStart(12, '0')}`, display_name: `${first} ${last}`, mail, upn: mail, aliases: [mail], kind: 'user', enabled: true, licensed: true, ...over };
	};
	const shared = (n, name, local) => {
		const mail = `${local}@contoso.example`;
		return { id: `7c9e${String(n).padStart(4, '0')}-6b2a-4d3c-9e1f-${String(n).padStart(12, '0')}`, display_name: name, mail, upn: mail, aliases: [mail], kind: 'shared', enabled: false, licensed: false };
	};
	const mailboxes = [
		u(1, 'Camille', 'Dupont'),
		u(2, 'Hugo', 'Martin'),
		u(3, 'Lea', 'Bernard', { display_name: 'Léa Bernard' }),
		u(4, 'Nathan', 'Petit'),
		u(5, 'Chloe', 'Robert', { display_name: 'Chloé Robert' }),
		u(6, 'Louis', 'Richard'),
		u(7, 'Emma', 'Durand'),
		u(8, 'Jules', 'Moreau'),
		u(9, 'Manon', 'Simon'),
		u(10, 'Arthur', 'Laurent', { mail: 'a.laurent@contoso.example', upn: 'a.laurent@contoso.example', aliases: ['a.laurent@contoso.example', 'arthur@contoso.example'] }),
		u(11, 'Ines', 'Garcia', { display_name: 'Inès Garcia', mail: 'ines@contoso.example', upn: 'ines.garcia@contoso.example', aliases: ['ines@contoso.example', 'ines.garcia@contoso.example'] }),
		u(12, 'Paul', 'Roux'),
		u(13, 'Marc', 'Fabre', { enabled: false }),
		u(14, 'Alice', 'Martin'),
		u(15, 'Theo', 'Blanc', { display_name: 'Théo Blanc', aliases: ['theo.blanc@contoso.example', 'alice.martin@contoso.example'] }),
		shared(16, 'Support', 'support'),
		shared(17, 'Accounting', 'accounting'),
		{ ...shared(18, 'Meeting room Paris', 'room.paris'), kind: 'room' },
		{ ...shared(19, 'Projector kit', 'projector'), kind: 'equipment' }
	];
	return { mailboxes, hidden: { guests: 1, no_mailbox: 2 } };
}

function tenantCheck(config, secrets) {
	const secret = secrets?.client_secret ?? '';
	if (!secret) return { ok: false, checks: [{ key: 'token', ok: false, level: 'required', msg: 'No client secret given.' }] };
	if (secret.startsWith('wrong')) return { ok: false, checks: [{ key: 'token', ok: false, level: 'required', msg: 'Wrong or expired client secret (AADSTS7000215).' }] };
	const checks = [{ key: 'token', ok: true, level: 'required', msg: 'Token obtained for the tenant.' }];
	const roles = (label, key, level, ok, role, extra = {}) => checks.push({ key, ok, level, msg: ok ? `${label} permission granted.` : `${label} permission missing: grant ${role} and admin consent.`, ...(ok ? {} : { missing: [role] }), ...extra });
	roles('Directory', 'directory', 'required', true, 'User.Read.All');
	roles('Mail', 'mail', 'required', true, 'Mail.Read');
	roles('Calendars', 'calendars', 'required', true, 'Calendars.Read');
	roles('Contacts', 'contacts', 'required', true, 'Contacts.Read');
	roles('Mailbox settings', 'settings', 'required', true, 'MailboxSettings.Read');
	roles('Tasks', 'tasks', 'optional', secret.startsWith('full'), 'Tasks.Read.All');
	checks.push({ key: 'probe_directory', ok: true, level: 'required', msg: 'The directory can be read.' });
	checks.push({ key: 'probe_mailbox', ok: true, level: 'required', msg: 'Exchange accepts the application on a mailbox.' });
	return { ok: true, checks, tenant_name: 'Contoso' };
}

function tenantView(c) {
	const accounts = [...state.accounts.values()].filter((a) => a.source?.connection?.id === c.id).length;
	return { id: c.id, name: c.name, kind: c.kind, config: c.config, has_secret: Object.keys(c.secrets).length > 0, notes: c.notes, last_test: c.last_test, last_test_result: c.last_test_result, accounts, provisioned: c.provisioned ?? null, created_at: c.created_at };
}

function tenantConfig(body) {
	const fields = CATALOGUE.find((x) => x.kind === 'm365_graph').source.fields;
	const conn = fields.filter((f) => f.scope === 'connection');
	for (const k of Object.keys(body.config ?? {})) if (!conn.some((f) => f.name === k && !f.secret)) throw new Error(`unknown field '${k}'`);
	const config = {};
	for (const f of conn.filter((x) => !x.secret)) {
		const v = String(body.config?.[f.name] ?? '').trim() || f.default;
		if (!v) throw new Error(`'${f.name}' is required`);
		config[f.name] = v;
	}
	return config;
}

function tenantSecrets(current, given) {
	let secrets = { ...current };
	if (given !== undefined) {
		if (!Object.keys(given).length) secrets = {};
		else for (const [k, v] of Object.entries(given)) {
			if (k !== 'client_secret') throw new Error(`unknown secret '${k}'`);
			if (v) secrets[k] = v;
			else delete secrets[k];
		}
	}
	return secrets;
}

function matchAccount(a, conn) {
	const free = a.source?.connection?.id === conn.id || (!a.source?.kind && !(a.pst_id && state.psts.get(a.pst_id)?.status === 'valid'));
	return { id: a.id, email: a.email, status: a.status, last_test: a.last_test, last_test_result: a.last_test_result, source_kind: a.source?.kind ?? (a.pst_id ? 'pst' : null), source_connection_id: a.source?.connection?.id ?? null, free };
}

/** Pure matching, same states as app/server/tenant_match.py. */
function tenantMatch(conn, server) {
	const { mailboxes, hidden } = tenantDirectory();
	const accounts = [...state.accounts.values()].filter((a) => a.server_id === server.id);
	// An account linked to this tenant keeps the mailbox it reads, whatever its address (a destination chosen by hand).
	const pinned = new Map();
	for (const a of [...accounts].sort((x, y) => x.id - y.id)) {
		const value = a.source?.connection?.id === conn.id ? String(a.source.config?.mailbox ?? '').toLowerCase() : '';
		const mb = value && mailboxes.find((x) => [x.id, x.mail, x.upn].some((v) => v && v.toLowerCase() === value));
		if (mb && !pinned.has(mb)) pinned.set(mb, a);
	}
	const taken = new Set([...pinned.values()].map((a) => a.id));
	const byEmail = new Map(accounts.filter((a) => !taken.has(a.id)).map((a) => [a.email.toLowerCase(), a]));
	const claims = new Map();
	for (const mb of mailboxes) {
		if (pinned.has(mb)) continue;
		const keys = [[mb.mail, 'mail'], [mb.upn, 'upn'], ...mb.aliases.map((x) => [x, 'alias'])];
		const hit = keys.find(([addr]) => byEmail.has(addr.toLowerCase()));
		if (!hit) continue;
		const account = byEmail.get(hit[0].toLowerCase());
		const list = claims.get(account.id) ?? [];
		list.push({ mb, by: hit[1] });
		claims.set(account.id, list);
	}
	const tasksOk = !!conn.last_test_result?.checks?.find((c) => c.key === 'tasks')?.ok;
	const coverage = (a) => {
		const tested = a.last_test_result;
		const dav = (kind, opt) => (!server.dav_enabled ? 'no_dav' : a.options?.[opt] === false ? 'off' : !tested ? 'unknown' : tested[kind]?.ok === false ? 'failed' : 'on');
		return { mail: a.options?.mail === false ? 'off' : 'on', calendar: dav('caldav', 'calendar'), contacts: dav('carddav', 'contacts'), tasks: !server.dav_enabled ? 'no_dav' : !tasksOk ? 'no_permission' : a.options?.calendar === false ? 'off' : dav('caldav', 'calendar') };
	};
	const lastJob = (a) => {
		const j = [...state.jobs.values()].filter((x) => x.account_id === a.id).sort((x, y) => y.id - x.id)[0];
		return j ? { id: j.id, status: j.status } : null;
	};
	const rows = [];
	const used = new Set();
	for (const mb of mailboxes) {
		const pin = pinned.get(mb);
		if (pin) {
			const email = pin.email.toLowerCase();
			const by = (mb.mail ?? '').toLowerCase() === email ? 'mail' : (mb.upn ?? '').toLowerCase() === email ? 'upn' : (mb.aliases ?? []).some((x) => x.toLowerCase() === email) ? 'alias' : 'manual';
			const job = lastJob(pin);
			const st = by !== 'manual' || job ? 'linked' : 'ready';
			rows.push({ state: st, mailbox: mb, account: matchAccount(pin, conn), matched_by: by, matched_address: by === 'upn' || by === 'alias' ? email : null, coverage: coverage(pin), last_job: st === 'linked' ? job : null });
			continue;
		}
		const found = [...claims.entries()].find(([, list]) => list.some((c) => c.mb === mb));
		if (!found) {
			rows.push({ state: 'no_target', mailbox: mb, account: null, matched_by: null, matched_address: null, coverage: { mail: 'unknown', calendar: 'unknown', contacts: 'unknown', tasks: 'unknown' }, last_job: null });
			continue;
		}
		const account = state.accounts.get(found[0]);
		const list = found[1];
		const mine = list.find((c) => c.mb === mb);
		const winner = [...list].sort((x, y) => (x.by === 'mail' ? 0 : 1) - (y.by === 'mail' ? 0 : 1))[0];
		const view = matchAccount(account, conn);
		let st;
		if (list.length > 1 && winner.mb !== mb) st = 'conflict';
		else if (account.source?.connection?.id === conn.id) st = 'linked';
		else if (account.source?.kind || (account.pst_id && state.psts.get(account.pst_id)?.status === 'valid')) st = 'other_source';
		else st = 'ready';
		if (st !== 'conflict') used.add(account.id);
		rows.push({ state: st, mailbox: mb, account: view, matched_by: mine.by, matched_address: mine.by === 'mail' ? null : account.email.toLowerCase(), ...(st === 'conflict' ? { conflict_with: winner.mb.mail } : {}), coverage: coverage(account), last_job: st === 'linked' ? lastJob(account) : null });
	}
	for (const a of accounts) if (!claims.has(a.id) && !taken.has(a.id)) rows.push({ state: 'no_source', mailbox: null, account: matchAccount(a, conn), matched_by: null, matched_address: null, coverage: coverage(a), last_job: null });
	const counts = {};
	for (const r of rows) counts[r.state] = (counts[r.state] ?? 0) + 1;
	const t = server.last_test_result;
	return { connection: { id: conn.id, name: conn.name }, server: { id: server.id, name: server.name, dav_enabled: !!server.dav_enabled, caldav_ok: t ? !!t.caldav?.ok : null, carddav_ok: t ? !!t.carddav?.ok : null }, rows, counts, fetched_at: Date.now() / 1000 - 120, purpose_checked: true, hidden };
}

// A flow answers one step further on each poll: waiting, then the steps, then done (or the failure chosen with /__mock/provision).
const PROVISION_STEPS = ['signin', 'app', 'consent', 'propagation', 'test'];
function flowView(flow) {
	flow.polls++;
	if (flow.fail === 'expired' && flow.polls > 1) return { status: 'expired', step: null, error: 'The code expired before the sign-in: start again.', error_code: 'expired', connection: null };
	if (flow.fail === 'admin' && flow.polls > 2) return { status: 'error', step: 'app', error: 'Creating the application was refused: the account must be Global Administrator or Application Administrator of the client tenant (Graph 403 Authorization_RequestDenied: Insufficient privileges to complete the operation.).', error_code: 'not_admin', connection: null };
	if (flow.remove) {
		if (flow.polls <= 1) return { status: 'waiting', step: 'signin', error: null, error_code: null, connection: null };
		if (flow.polls === 2) return { status: 'working', step: 'remove', error: null, error_code: null, connection: null };
		const conn = state.sourceConnections.get(flow.connectionId);
		if (conn) conn.provisioned = null;
		return { status: 'done', step: 'remove', error: null, error_code: null, connection: conn ? tenantView(conn) : null };
	}
	if (flow.polls <= 1) return { status: 'waiting', step: 'signin', error: null, error_code: null, connection: null };
	const step = PROVISION_STEPS[Math.min(flow.polls - 1, PROVISION_STEPS.length - 1)];
	if (flow.polls <= PROVISION_STEPS.length) return { status: 'working', step, error: null, error_code: null, connection: null };
	if (!flow.connectionId) {
		const conn = { id: state.tenantNext++, name: flow.name || 'Fabrikam', kind: 'm365_graph', config: { tenant_id: '3c9e7a10-5b42-4d18-8f6a-1e2d4c7b9a05', client_id: 'a7d5e2b4-9c18-4f03-b6a1-8e0d3f5c2b79', cloud: flow.cloud || 'global' }, secrets: { client_secret: 'full-provisioned' }, notes: null, last_test: Date.now() / 1000, last_test_result: tenantCheck({}, { client_secret: 'full-provisioned' }), provisioned: { app_object_id: 'f1e2d3c4-b5a6-4978-8a9b-0c1d2e3f4a5b', sp_object_id: '0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d', created_at: Date.now() / 1000 }, created_at: Date.now() / 1000 };
		state.sourceConnections.set(conn.id, conn);
		flow.connectionId = conn.id;
	}
	const conn = state.sourceConnections.get(flow.connectionId);
	if (flow.fail === 'propagation' && !conn.healed) {
		// The permissions are not active yet: saved, last check failing.
		conn.last_test_result = { ok: false, checks: [{ key: 'token', ok: false, level: 'required', msg: 'Microsoft has not activated the new credentials yet.' }], tenant_name: conn.name };
		return { status: 'done', step: 'propagation', error: 'The new credentials do not work yet (Microsoft can take a few minutes to propagate them). The tenant is saved: run Test again later.', error_code: 'propagation_timeout', connection: tenantView(conn) };
	}
	return { status: 'done', step: 'test', error: null, error_code: null, connection: tenantView(conn) };
}

function startFlow(extra) {
	const flow = { id: `flow-${state.flowNext++}`, polls: 0, cancelled: false, fail: state.provisionFail, ...extra };
	state.flows.set(flow.id, flow);
	state.provisionCalls.push({ ...extra });
	return send(201, { flow_id: flow.id, user_code: 'ABCD-EFGH', verification_uri: 'https://microsoft.com/devicelogin', expires_in: 900, interval: 2, message: 'To sign in, open https://microsoft.com/devicelogin and enter the code ABCD-EFGH.' });
}

/** Two stored `mailbox` values naming the same tenant mailbox (its id, address or sign-in name). */
function sameMailbox(a, b) {
	const names = (v) => {
		const key = String(v ?? '').toLowerCase();
		const mb = tenantDirectory().mailboxes.find((x) => [x.id, x.mail, x.upn].some((n) => n && n.toLowerCase() === key));
		return mb ? mb.id : key;
	};
	return names(a) === names(b);
}

function mailboxName(v) {
	const key = String(v ?? '').toLowerCase();
	return tenantDirectory().mailboxes.find((x) => [x.id, x.mail, x.upn].some((n) => n && n.toLowerCase() === key))?.mail ?? String(v);
}

function handleTenants(parts, m, json, query) {
	const C = state.sourceConnections;
	if (parts[0] === 'provision') {
		if (!state.provisionOn) return fail(409, 'Connect with Microsoft is not configured');
		if (parts.length === 1 && m === 'POST') {
			const body = json();
			return startFlow({ cloud: body.cloud ?? null, name: String(body.name ?? '').trim() || null });
		}
		const flow = state.flows.get(parts[1]);
		if (!flow) return fail(404, 'unknown flow');
		if (m === 'DELETE') {
			flow.cancelled = true;
			return send(200, { ok: true });
		}
		// 'lost': mccb restarted meanwhile, the flow is no longer known
		if (m === 'GET') return flow.cancelled || (flow.fail === 'lost' && flow.polls++ > 0) ? fail(404, 'unknown flow') : send(200, flowView(flow));
	}
	if (!parts.length) {
		if (m === 'GET') return send(200, [...C.values()].map(tenantView));
		const body = json();
		let config;
		let secrets;
		try {
			if (body.kind !== 'm365_graph') throw new Error('this connector has no directory');
			config = tenantConfig(body);
			secrets = tenantSecrets({}, body.secrets);
			if (!secrets.client_secret) throw new Error("'client_secret' is required");
			if (!String(body.name ?? '').trim()) throw new Error("'name' is required");
		} catch (error) {
			return fail(422, error.message);
		}
		const conn = { id: state.tenantNext++, name: body.name.trim(), kind: body.kind, config, secrets, notes: body.notes ?? null, last_test: null, last_test_result: null, created_at: Date.now() / 1000 };
		C.set(conn.id, conn);
		return send(201, tenantView(conn));
	}
	if (parts[0] === 'test' && m === 'POST') {
		const body = json();
		const stored = body.id != null ? C.get(Number(body.id)) : null;
		let result;
		try {
			tenantConfig(body);
			result = tenantCheck(body.config, tenantSecrets(stored?.secrets ?? {}, body.secrets));
		} catch (error) {
			result = { ok: false, checks: [{ key: 'token', ok: false, level: 'required', msg: error.message }] };
		}
		state.sourceTests.push({ id: body.id ?? null, kind: body.kind, config: body.config, has_secret: !!(body.secrets && Object.keys(body.secrets).length) });
		return send(200, result);
	}
	const conn = C.get(Number(parts[0]));
	if (!conn) return fail(404, 'unknown source connection');
	if (parts[1] === 'deprovision' && m === 'POST') {
		if (!state.provisionOn || !conn.provisioned) return fail(409, 'this connection was not created by Connect with Microsoft');
		return startFlow({ remove: true, connectionId: conn.id });
	}
	if (parts[1] === 'test' && m === 'POST') {
		conn.healed = true;
		const result = tenantCheck(conn.config, conn.secrets);
		state.sourceTests.push({ id: conn.id, kind: conn.kind, config: conn.config, has_secret: true });
		conn.last_test = Date.now() / 1000;
		conn.last_test_result = result;
		return send(200, result);
	}
	if ((parts[1] === 'mailboxes' || parts[1] === 'match') && m === 'GET' && state.tenantFail) return fail(502, state.tenantFail);
	if (parts[1] === 'mailboxes' && m === 'GET') {
		const { mailboxes, hidden } = tenantDirectory();
		return send(200, { fetched_at: Date.now() / 1000 - (query.get('refresh') === '1' ? 0 : 120), purpose_checked: true, mailboxes, hidden });
	}
	if (parts[1] === 'match' && m === 'GET') {
		const server = state.servers.get(Number(query.get('server_id')));
		if (!server) return fail(404, 'unknown server');
		return send(200, tenantMatch(conn, server));
	}
	if (parts[1] === 'link' && m === 'POST') {
		const body = json();
		state.linkCalls.push(body);
		const server = state.servers.get(Number(body.server_id));
		if (!server) return fail(404, 'unknown server');
		const tasksOk = !!conn.last_test_result?.checks?.find((c) => c.key === 'tasks')?.ok;
		const include_tasks = (body.include_tasks ?? true) && tasksOk;
		const linked = [];
		const errors = [];
		for (const l of body.links ?? []) {
			const account = state.accounts.get(Number(l.account_id));
			if (!account || account.server_id !== server.id) errors.push({ account_id: l.account_id, msg: 'not a mailbox of this server' });
			else if ([...state.jobs.values()].some((j) => j.account_id === account.id && ['queued', 'running', 'paused', 'stopping'].includes(j.status))) errors.push({ account_id: account.id, msg: 'a job is active for this mailbox' });
			else if (account.source?.connection?.id === conn.id && !sameMailbox(account.source.config?.mailbox, l.mailbox_id)) errors.push({ account_id: account.id, msg: `already the destination of ${mailboxName(account.source.config?.mailbox)}: set that one back to automatic first` });
			else if (l.manual && account.source?.connection?.id !== conn.id && (account.source?.kind || (account.pst_id && state.psts.get(account.pst_id)?.status === 'valid'))) errors.push({ account_id: account.id, msg: 'the mailbox has another source' });
			else {
				if (l.manual)
					for (const other of state.accounts.values())
						if (other.id !== account.id && other.server_id === server.id && other.source?.connection?.id === conn.id && sameMailbox(other.source.config?.mailbox, l.mailbox_id)) {
							other.source = undefined;
							publish('account', accountView(other));
						}
				account.source = { kind: 'm365_graph', config: { mailbox: l.mailbox_id, include_tasks }, has_secret: false, connection: { id: conn.id, name: conn.name } };
				account.srcSecrets = {};
				account.pst_id = null;
				linked.push(account.id);
				publish('account', accountView(account));
			}
		}
		if (errors.length && !linked.length) return fail(409, { msg: 'no mailbox linked', errors });
		let batch = null;
		const jobs = [];
		if (body.start && linked.length) {
			if (body.batch_name) {
				batch = { id: state.next2.batch++, name: body.batch_name, created_at: Date.now() / 1000 };
				state.batches.set(batch.id, batch);
			}
			for (const id of linked) {
				const job = startJob(state.accounts.get(id));
				if (batch) job.batch_id = batch.id;
				if (body.mode) job.mode = body.mode;
				publish('job', job);
				jobs.push(job);
			}
		}
		return send(200, { linked, errors, jobs: jobs.map((j) => ({ ...j })), batch_id: batch?.id ?? null });
	}
	if (parts.length === 1) {
		if (m === 'GET') return send(200, tenantView(conn));
		if (m === 'PUT') {
			const body = json();
			try {
				const config = tenantConfig({ config: { ...conn.config, ...(body.config ?? {}) } });
				const linked = tenantView(conn).accounts;
				if (linked && (config.tenant_id !== conn.config.tenant_id || config.cloud !== conn.config.cloud)) return fail(409, 'mailboxes are linked to this tenant: create another tenant');
				const secrets = tenantSecrets(conn.secrets, body.secrets);
				const changed = JSON.stringify(config) !== JSON.stringify(conn.config) || JSON.stringify(secrets) !== JSON.stringify(conn.secrets);
				Object.assign(conn, { config, secrets, name: body.name?.trim() || conn.name, notes: 'notes' in body ? body.notes : conn.notes });
				if (changed) Object.assign(conn, { last_test: null, last_test_result: null });
			} catch (error) {
				return fail(422, error.message);
			}
			return send(200, tenantView(conn));
		}
		if (m === 'DELETE') {
			const n = tenantView(conn).accounts;
			if (n) return fail(409, { msg: `${n} account(s) are linked to this connection`, accounts: n });
			C.delete(conn.id);
			return send(200, { ok: true });
		}
	}
	return null;
}

/** A tenant already registered, a server for it, and target mailboxes covering every match state. */
function seedTenant() {
	const now = Date.now() / 1000;
	const ago = (minutes) => now - minutes * 60;
	const dir = tenantDirectory().mailboxes;
	const by = (local) => dir.find((x) => x.mail.startsWith(`${local}@`));
	const checks = tenantCheck({}, { client_secret: 'seed' });
	state.sourceConnections = new Map([[1, { id: 1, name: 'Contoso', kind: 'm365_graph', config: { tenant_id: '8d1f3c52-4a7e-4b60-9c2d-5e0a1b7f6d34', client_id: 'c4b2a9e1-7d3f-4a58-b6c0-2f9e8d1a0b57', cloud: 'global' }, secrets: { client_secret: 'seed' }, notes: 'Fictional demo tenant.', last_test: ago(35), last_test_result: checks, created_at: ago(2000) }]]);
	state.tenantNext = 2;
	state.tenantOn = true;
	state.servers.set(3, makeServer(3, 'Yeti (Contoso)', 'mail.contoso.example', { caldav_url: 'https://dav.contoso.example/', carddav_url: 'https://dav.contoso.example/', last_test: ago(40), last_test_result: { imap: { ok: true, msg: 'IMAP server reachable', capabilities: [] }, caldav: { ok: true, msg: 'reachable (HTTP 207)' }, carddav: { ok: true, msg: 'reachable (HTTP 207)' } } }));
	const tested = (over = {}) => ({ status: 'ok', last_test: ago(30), last_test_result: { imap: { ok: true, msg: 'IMAP connection successful', quota: null }, caldav: { ok: true, msg: 'CalDAV reachable' }, carddav: { ok: true, msg: 'CardDAV reachable' } }, ...over });
	const conn = { id: 1, name: 'Contoso' };
	const linkedSource = (mb) => ({ kind: 'm365_graph', config: { mailbox: mb.id, include_tasks: false }, has_secret: false, connection: conn });
	let id = Math.max(0, ...state.accounts.keys()) + 1;
	const add = (email, over = {}, serverId = 3) => {
		const a = makeAccount(id, email, serverId, { ...tested(), ...over });
		state.accounts.set(id++, a);
		return a;
	};
	// ready
	for (const l of ['camille.dupont', 'louis.richard', 'emma.durand', 'manon.simon', 'paul.roux', 'marc.fabre', 'support', 'accounting']) add(`${l}@contoso.example`);
	add('alice.martin@contoso.example');
	add('arthur@contoso.example'); // matched by alias
	add('ines.garcia@contoso.example', { last_test: null, last_test_result: null, status: 'new' }); // matched by UPN, never tested
	add('chloe.robert@contoso.example', { options: { mail: true, calendar: false, contacts: true } });
	// linked, one with a finished job
	const hugo = add('hugo.martin@contoso.example', { source: linkedSource(by('hugo.martin')) });
	const lea = add('lea.bernard@contoso.example', { source: linkedSource(by('lea.bernard')), last_test_result: { imap: { ok: true, msg: 'IMAP connection successful' }, caldav: { ok: false, msg: 'CalDAV: 401 Unauthorized' }, carddav: { ok: true, msg: 'CardDAV reachable' } } });
	// other source: a PST and an IMAP source
	add('nathan.petit@contoso.example', { pst_id: 1 });
	// target without source mailbox
	add('stagiaire@contoso.example');
	add('scanner@contoso.example');
	// the same account on the DAV-less server, to show the banner
	for (const l of ['camille.dupont', 'hugo.martin', 'emma.durand']) add(`${l}@contoso.example`, {}, 2);
	for (const [a, started] of [[hugo, 90], [lea, 85]]) {
		const jid = state.next.job++;
		const progress = emptyProgress();
		for (const k of Object.keys(progress)) progress[k] = { done: 100, total: 100, errors: 0 };
		state.jobs.set(jid, { id: jid, account_id: a.id, status: 'done', phase: 'verify', progress, current_folder: null, rate: null, rate_bytes: null, eta: 0, started_at: ago(started), finished_at: ago(started - 20), error: null, mode: 'full', created_at: ago(started + 1), tuning: null, priority: 0, batch_id: null, pause_reason: null });
		state.logs.set(jid, []);
	}
	state.next.account = Math.max(state.next.account, ...state.accounts.keys()) + 1;
	state.next.server = Math.max(state.next.server, 4);
}
