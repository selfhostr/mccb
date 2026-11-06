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
	return { id, name, imap_host: host, imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: null, caldav_url: null, carddav_url: null, dav_enabled: true, workers: 4, notes: '', tuning: 'auto', builders: null, ...over };
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
		planDelay: options.planDelay ?? 600,
		plans: new Map(),
		compares: new Map(),
		batches: new Map(),
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
}
reset();

function publish(kind, data) {
	for (const fn of listeners) fn(kind, data);
}

function accountView(a) {
	const job = [...state.jobs.values()].filter((j) => j.account_id === a.id).sort((x, y) => y.id - x.id)[0] ?? null;
	const { srcSecrets, tgtSecrets, ...rest } = a;
	const source = a.source ?? { kind: 'pst', config: { pst_id: a.pst_id }, has_secret: false };
	return { ...rest, source: a.source ? { ...a.source, has_secret: !!Object.keys(srcSecrets ?? {}).length } : { ...source, config: { pst_id: a.pst_id } }, target: { ...a.target, has_secret: !!Object.keys(tgtSecrets ?? {}).length }, job };
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
	if (!pst || pst.status !== 'valid') return { ...base, status: 'error', source: { kind: 'pst', ok: false, msg: 'no PST linked to this account' } };
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
	if (path === '/__mock/state') return send(200, { testCalls: state.testCalls, runCalls: state.runCalls, uploadOrder: state.uploadOrder, intake: state.intake, intakePuts: state.intakePuts, notifyTests: state.notifyTests, routeCalls: state.routeCalls, probeCalls: state.probeCalls, fetchCalls: state.fetchCalls, cancelCalls: state.cancelCalls, retryCalls: state.retryCalls, accounts: [...state.accounts.values()].map(accountView), runBodies: state.runBodies, reorderCalls: state.reorderCalls, deltaCalls: state.deltaCalls, compareCalls: state.compareCalls, tokens: state.tokens, policies: { server: Object.fromEntries(state.policies.server), account: Object.fromEntries(state.policies.account) } });
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
		if (parts[2] === 'test') return send(200, { imap: { ok: true, msg: `IMAP server reachable (${server.imap_host}:${server.imap_port})`, capabilities: [] }, caldav: { ok: server.dav_enabled, msg: server.dav_enabled ? 'reachable (HTTP 207)' : 'DAV disabled for this server' }, carddav: { ok: false, msg: 'https://dav.example.com/ unreachable: timeout' } });
		if (m === 'GET') return send(200, server);
		if (m === 'PUT') {
			Object.assign(server, json(), { id: server.id });
			return send(200, server);
		}
		if (m === 'DELETE') {
			state.servers.delete(server.id);
			for (const a of [...state.accounts.values()]) if (a.server_id === server.id) state.accounts.delete(a.id);
			return send(200, { ok: true });
		}
	}

	if (path === '/connectors' && m === 'GET') return send(200, CATALOGUE);
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
			if ([...state.accounts.values()].some((a) => a.server_id === body.server_id && a.email === body.email)) return fail(409, 'this account already exists on this server');
			const id = state.next.account++;
			const account = makeAccount(id, body.email, body.server_id, { login: body.login || body.email, has_password: !!body.password });
			state.accounts.set(id, account);
			publish('account', accountView(account));
			return send(201, accountView(account));
		}
		const account = state.accounts.get(Number(parts[1]));
		if (!account) return fail(404, 'unknown account');
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
			if ([...state.jobs.values()].some((j) => j.account_id === account.id && ['queued', 'running', 'paused', 'stopping'].includes(j.status))) return fail(409, 'a job is active for this account');
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
		if (unknown.length) return fail(422, { msg: 'unknown account(s)', account_ids: unknown });
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

	state.next.account = 11;
	state.next.pst = 11;
	state.next.server = 3;
	state.next2.batch = 2;
}
