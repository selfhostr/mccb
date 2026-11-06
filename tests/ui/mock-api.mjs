#!/usr/bin/env node
/**
 * Mock API for the UI: same contract as app/server/web.py (docs/api.md), Node stdlib only, fictional data.
 * Serves the build from app/web. Password: admin.
 *   node tests/ui/mock-api.mjs [port]     (npm run mock, from web/)
 * Tests drive it through /__mock/*: reset the state, change delays, push events.
 */
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.argv[2] ?? process.env.PORT ?? 4173);
const WEB = join(dirname(fileURLToPath(import.meta.url)), '../../app/web');
const CHUNK = 1024 * 1024;
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.json': 'application/json' };

const sessions = new Set();
const clients = new Set();
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

function reset(options = {}) {
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
		retryCalls: []
	};
}
reset();

function publish(kind, data) {
	const text = `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
	for (const res of clients) res.write(text);
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

function startJob(account) {
	const id = state.next.job++;
	const progress = emptyProgress();
	progress.mail.total = 900;
	progress.calendar.total = 120;
	progress.contacts.total = 80;
	const job = { id, account_id: account.id, status: 'queued', phase: 'precheck', progress, current_folder: null, rate: null, rate_bytes: null, eta: null, started_at: null, finished_at: null, error: null, mode: 'full', created_at: Date.now() / 1000, tuning: null };
	state.jobs.set(id, job);
	state.logs.set(id, []);
	log(job, 'info', 'Job queued');
	run(job);
	return job;
}

function run(job) {
	const timer = setInterval(() => {
		if (job.status === 'paused') return;
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
			p.done = Math.min(p.total, p.done + Math.ceil(p.total / 4));
			job.current_folder = kind === 'mail' ? 'Inbox' : kind;
			job.rate = 42.5;
			job.rate_bytes = 1_500_000;
			job.eta = 12;
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
	}, state.jobTick);
	timers.add(timer);
}

const send = (res, code, body, headers = {}) => {
	const text = typeof body === 'string' ? body : JSON.stringify(body);
	res.writeHead(code, { 'Content-Type': typeof body === 'string' ? 'text/plain' : 'application/json', ...headers });
	res.end(text);
};
const fail = (res, code, detail) => send(res, code, { detail });

async function readBody(req) {
	const parts = [];
	for await (const part of req) parts.push(part);
	return Buffer.concat(parts);
}

function serveStatic(res, path) {
	const clean = normalize(path).replace(/^(\.\.[/\\])+/, '');
	let file = join(WEB, clean);
	if (!existsSync(file) || !statSync(file).isFile()) file = join(WEB, 'index.html');
	if (!existsSync(file)) return send(res, 200, 'Build missing: run npm run build in web/');
	res.writeHead(200, { 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' });
	createReadStream(file).pipe(res);
}

function authed(req) {
	return (req.headers.cookie ?? '').split(';').some((c) => sessions.has(c.trim().replace('mccb_session=', '')));
}

async function control(req, res, path) {
	const body = JSON.parse((await readBody(req)).toString() || '{}');
	if (path === '/__mock/reset') {
		reset(body);
		return send(res, 200, { ok: true });
	}
	if (path === '/__mock/state') return send(res, 200, { testCalls: state.testCalls, runCalls: state.runCalls, uploadOrder: state.uploadOrder, intake: state.intake, intakePuts: state.intakePuts, notifyTests: state.notifyTests, routeCalls: state.routeCalls, probeCalls: state.probeCalls, fetchCalls: state.fetchCalls, cancelCalls: state.cancelCalls, retryCalls: state.retryCalls, accounts: [...state.accounts.values()].map(accountView) });
	if (path === '/__mock/inbox') {
		state.inbox = body.files ?? [];
		return send(res, 200, { ok: true });
	}
	if (path === '/__mock/pst') {
		// Insert or update a PST row, and tell the stream: what the worker does while fetching.
		const current = state.psts.get(body.pst.id) ?? {};
		const pst = { ...validPst(body.pst.id, body.pst.filename ?? current.filename ?? 'x.pst', body.pst.size ?? 0), ...current, ...body.pst };
		state.psts.set(pst.id, pst);
		publish('pst', pst);
		return send(res, 200, pst);
	}
	if (path === '/__mock/event') {
		publish(body.kind, body.data);
		return send(res, 200, { ok: true });
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
		return send(res, 200, { ok: true });
	}
	return fail(res, 404, 'unknown control');
}

async function api(req, res, url) {
	const path = url.pathname.slice(4);
	const m = req.method;
	const parts = path.split('/').filter(Boolean);
	if (path === '/health') return send(res, 200, { ok: true, worker: { alive: true, last_seen: Date.now() / 1000 }, time: Date.now() / 1000, version: '0.0.0-mock' });
	if (path === '/version') return send(res, 200, { version: '0.0.0-mock' });
	if (path === '/logout') {
		await readBody(req);
		return send(res, 200, { ok: true }, { 'Set-Cookie': 'mccb_session=; Path=/; Max-Age=0' });
	}
	if (path === '/login' && m === 'POST') {
		const body = JSON.parse((await readBody(req)).toString() || '{}');
		if (body.password !== 'admin') return fail(res, 401, 'incorrect password');
		const sid = randomUUID();
		sessions.add(sid);
		return send(res, 200, { ok: true, authenticated: true }, { 'Set-Cookie': `mccb_session=${sid}; Path=/; HttpOnly; SameSite=Lax` });
	}
	if (!authed(req)) {
		await readBody(req);
		return fail(res, 401, 'not authenticated');
	}
	if (path === '/me') return send(res, 200, { authenticated: true, user: 'admin' });
	if (path === '/events') {
		res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
		res.write('retry: 3000\n\n');
		clients.add(res);
		req.on('close', () => clients.delete(res));
		return undefined;
	}
	const raw = await readBody(req);
	const json = () => JSON.parse(raw.toString() || '{}');

	if (parts[0] === 'servers') {
		if (parts.length === 1) {
			if (m === 'GET') return send(res, 200, [...state.servers.values()]);
			const id = state.next.server++;
			const server = { ...makeServer(id, '', ''), ...json(), id };
			state.servers.set(id, server);
			return send(res, 201, server);
		}
		const server = state.servers.get(Number(parts[1]));
		if (!server) return fail(res, 404, 'unknown server');
		if (parts[2] === 'test') return send(res, 200, { imap: { ok: true, msg: `IMAP server reachable (${server.imap_host}:${server.imap_port})`, capabilities: [] }, caldav: { ok: server.dav_enabled, msg: server.dav_enabled ? 'reachable (HTTP 207)' : 'DAV disabled for this server' }, carddav: { ok: false, msg: 'https://dav.example.com/ unreachable: timeout' } });
		if (m === 'GET') return send(res, 200, server);
		if (m === 'PUT') {
			Object.assign(server, json(), { id: server.id });
			return send(res, 200, server);
		}
		if (m === 'DELETE') {
			state.servers.delete(server.id);
			for (const a of [...state.accounts.values()]) if (a.server_id === server.id) state.accounts.delete(a.id);
			return send(res, 200, { ok: true });
		}
	}

	if (path === '/connectors' && m === 'GET') return send(res, 200, CATALOGUE);
	if (parts[0] === 'connectors' && parts[2] === 'test' && m === 'POST') {
		const body = json();
		const spec = CATALOGUE.find((c) => c.kind === parts[1]);
		if (!spec) return fail(res, 404, 'unknown connector');
		const account = body.account_id != null ? state.accounts.get(body.account_id) : null;
		const key = body.role === 'source' ? 'srcSecrets' : 'tgtSecrets';
		const current = account && account[body.role]?.kind === parts[1] ? account[key] : {};
		state.probeCalls.push({ kind: parts[1], body });
		try {
			checkEndpoint(parts[1], body.role ?? 'source', body.config, current, body.secrets);
		} catch (error) {
			return fail(res, 422, error.message);
		}
		const host = String(body.config?.host ?? body.config?.server ?? body.config?.mailbox ?? '');
		if (host.startsWith('bad')) return send(res, 200, { ok: false, error_kind: 'unreachable', msg: `cannot connect to ${host}` });
		if (host.startsWith('wrong')) return send(res, 200, { ok: false, error_kind: 'auth', msg: 'LOGIN failed for jane@example.com' });
		return body.role === 'target' ? send(res, 200, { ok: true, msg: 'connection successful', quota: null }) : send(res, 200, { ok: true, msg: '3 folder(s) found', folders: [{ path: 'INBOX', kind: 'mail', special: 'inbox', count: 12 }] });
	}

	if (path === '/intake/settings') {
		if (m === 'GET') return send(res, 200, state.intake);
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
			if (d.enabled && d.ntfy.enabled && !d.ntfy.topic) return fail(res, 422, 'ntfy.topic is required when ntfy is enabled');
			if (d.enabled && d.webhook.enabled && !d.webhook.url) return fail(res, 422, 'webhook.url is required when the webhook is enabled');
		}
		if (patch.inbox) Object.assign(next.inbox, patch.inbox);
		if (patch.auto_chain) {
			for (const dom of Object.values(patch.auto_chain.default_domains ?? {})) if (!/^[a-z0-9.-]+$/i.test(dom)) return fail(res, 422, `invalid domain: ${dom}`);
			Object.assign(next.auto_chain, patch.auto_chain);
		}
		state.intake = next;
		return send(res, 200, next);
	}
	if (path === '/intake/notifications/test' && m === 'POST') {
		const body = json();
		const cfg = state.intake.notifications;
		const names = body.channel ? [body.channel] : ['ntfy', 'webhook', 'email'].filter((c) => cfg[c].enabled);
		if (!names.length) return fail(res, 422, 'no channel enabled');
		state.notifyTests++;
		const results = Object.fromEntries(names.map((c) => [c, c === 'webhook' && cfg.webhook.url.includes('fail') ? { ok: false, error: 'ConnectError: connection refused' } : { ok: true, error: null }]));
		return send(res, 200, { ok: Object.values(results).every((x) => x.ok), results });
	}
	if (path === '/intake/inbox') return send(res, 200, { path: state.intake.inbox.path, enabled: state.intake.inbox.enabled, settle_seconds: state.intake.inbox.settle_seconds, files: state.inbox });
	if (path === '/psts/fetch' && m === 'POST') {
		const body = json();
		const need = { http: ['url'], sftp: ['host', 'path', 'username'], s3: ['endpoint', 'bucket', 'key', 'access_key', 'secret_key'] }[body.kind];
		if (!need) return fail(res, 422, 'kind must be http, sftp or s3');
		for (const f of need) if (!body[f]) return fail(res, 422, `${f} is required`);
		if (String(body.url ?? '').includes('169.254')) return fail(res, 422, 'destination refused: link-local address');
		state.fetchCalls.push(body);
		const id = state.next.pst++;
		const origin = body.url ? new URL(body.url).host : (body.host ?? body.endpoint);
		const pst = { id, filename: body.filename || String(body.url ?? body.path ?? body.key).split('/').pop(), size: 0, status: 'fetching', scan: null, error: null, pst_sha: null, created_at: Date.now() / 1000, updated_at: Date.now() / 1000, source: 'fetch', origin, received_bytes: 0, chain: null };
		state.psts.set(id, pst);
		publish('pst', pst);
		return send(res, 201, pst);
	}

	if (parts[0] === 'accounts') {
		if (parts[1] === 'csv-template') return send(res, 200, 'email;password;login\nalice@example.com;password;\n', { 'Content-Type': 'text/csv' });
		if (parts[1] === 'import-csv') return send(res, 200, { created: 1, updated: 0, errors: [{ line: 3, msg: 'invalid email address' }] });
		if (parts.length === 1) {
			if (m === 'GET') {
				const server = url.searchParams.get('server_id');
				return send(res, 200, [...state.accounts.values()].filter((a) => !server || a.server_id === Number(server)).sort((a, b) => a.email.localeCompare(b.email)).map(accountView));
			}
			const body = json();
			if ([...state.accounts.values()].some((a) => a.server_id === body.server_id && a.email === body.email)) return fail(res, 409, 'this account already exists on this server');
			const id = state.next.account++;
			const account = makeAccount(id, body.email, body.server_id, { login: body.login || body.email, has_password: !!body.password });
			state.accounts.set(id, account);
			publish('account', accountView(account));
			return send(res, 201, accountView(account));
		}
		const account = state.accounts.get(Number(parts[1]));
		if (!account) return fail(res, 404, 'unknown account');
		if (parts[2] === 'test') {
			state.testCalls[account.id] = (state.testCalls[account.id] ?? 0) + 1;
			await new Promise((r) => setTimeout(r, state.testDelay));
			const ok = !account.email.startsWith('bob');
			const result = { imap: { ok: true, msg: 'IMAP connection successful', quota: null }, caldav: { ok, msg: ok ? 'CalDAV reachable' : 'HTTP 401 from dav.example.com' }, carddav: { ok: true, msg: 'CardDAV reachable' } };
			account.status = ok ? 'ok' : 'error';
			account.last_test = Date.now() / 1000;
			account.last_test_result = result;
			publish('account', accountView(account));
			return send(res, 200, result);
		}
		if (parts[2] === 'source' || parts[2] === 'target') {
			const role = parts[2];
			if (m === 'GET') return send(res, 200, accountView(account)[role]);
			if ([...state.jobs.values()].some((j) => j.account_id === account.id && ['queued', 'running', 'paused', 'stopping'].includes(j.status))) return fail(res, 409, 'a job is active for this account');
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
					return fail(res, 422, error.message);
				}
			}
			publish('account', accountView(account));
			return send(res, 200, accountView(account));
		}
		if (parts[2] === 'pst') {
			account.pst_id = json().pst_id ?? null;
			publish('account', accountView(account));
			return send(res, 200, accountView(account));
		}
		if (m === 'GET') return send(res, 200, accountView(account));
		if (m === 'PUT') {
			const body = json();
			if (body.options) account.options = { ...account.options, ...body.options };
			if (body.password) account.status = 'new';
			if (body.login !== undefined) account.login = body.login || account.email;
			publish('account', accountView(account));
			return send(res, 200, accountView(account));
		}
		if (m === 'DELETE') {
			state.accounts.delete(account.id);
			return send(res, 200, { ok: true });
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
			return send(res, 200, { id: upload.id, chunk_size: CHUNK, received: [...upload.received] });
		}
		const upload = state.uploads.get(Number(parts[1]));
		if (!upload) return fail(res, 404, 'unknown upload');
		if (parts.length === 2) return send(res, 200, { id: upload.id, filename: upload.filename, size: upload.size, chunk_size: CHUNK, received: [...upload.received], status: upload.status, chunks: Math.max(1, Math.ceil(upload.size / CHUNK)), error: null });
		if (parts[2] === 'chunks') {
			if (state.chunkDelay) await new Promise((r) => setTimeout(r, state.chunkDelay));
			if (createHash('sha256').update(raw).digest('hex') !== req.headers['x-chunk-sha256']) return fail(res, 422, 'incorrect SHA-256');
			upload.received.add(Number(parts[3]));
			return send(res, 200, { ok: true, received: [...upload.received] });
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
			return send(res, 200, { id: upload.id, status: 'validating', pst_id: upload.id, pst });
		}
	}

	if (parts[0] === 'psts') {
		if (parts.length === 1) return send(res, 200, [...state.psts.values()]);
		const pst = state.psts.get(Number(parts[1]));
		if (!pst) return fail(res, 404, 'unknown PST');
		if (parts[2] === 'cancel' && m === 'POST') {
			state.cancelCalls.push(pst.id);
			if (pst.status !== 'fetching') return fail(res, 409, 'this PST is not being fetched');
			if (pst.slowCancel) {
				pst.status = 'cancelling';
				publish('pst', pst);
				return send(res, 200, { ok: true, state: 'cancelling' });
			}
			state.psts.delete(pst.id);
			publish('pst', { id: pst.id, deleted: true });
			return send(res, 200, { ok: true, state: 'removed' });
		}
		if (parts[2] === 'retry-fetch' && m === 'POST') {
			state.retryCalls.push(pst.id);
			if (pst.status !== 'fetch_failed') return fail(res, 409, 'only a failed fetch can be retried');
			Object.assign(pst, { status: 'fetching', error: null });
			publish('pst', pst);
			return send(res, 200, pst);
		}
		if (m === 'DELETE') {
			state.psts.delete(pst.id);
			state.uploads.delete(pst.id);
			return send(res, 200, { ok: true });
		}
		return send(res, 200, pst);
	}

	if (path === '/runs' && m === 'POST') {
		const ids = json().account_ids ?? [];
		state.runCalls.push(ids);
		const jobs = ids.map((id) => state.accounts.get(id)).filter(Boolean).map(startJob);
		for (const job of jobs) publish('job', job);
		return send(res, 200, jobs);
	}

	if (parts[0] === 'jobs') {
		if (parts.length === 1) return send(res, 200, [...state.jobs.values()]);
		const job = state.jobs.get(Number(parts[1]));
		if (!job) return fail(res, 404, 'unknown job');
		if (parts.length === 2) return send(res, 200, job);
		const action = parts[2];
		if (action === 'pause') job.status = 'paused';
		else if (action === 'resume') job.status = 'running';
		else if (action === 'stop') job.status = 'stopping';
		else if (action === 'retry' || action === 'retry-errors') {
			job.status = 'queued';
			job.finished_at = null;
			job.progress = { ...emptyProgress(), mail: { done: 0, total: 900, errors: 0 }, calendar: { done: 0, total: 120, errors: 0 }, contacts: { done: 0, total: 80, errors: 0 } };
			run(job);
		} else if (action === 'log') {
			const after = Number(url.searchParams.get('after') ?? 0);
			const lines = state.logs.get(job.id).filter((l) => l.n > after);
			return send(res, 200, { lines, next: lines.at(-1)?.n ?? after });
		} else if (action === 'errors') {
			return send(res, 200, [{ id: 1, kind: 'mail', folder: 'Inbox', item_key: 'k1', subject: 'Quarterly report', error: 'APPEND refused: message too large', updated_at: Date.now() / 1000 }]);
		} else if (action === 'report') {
			if (!['done', 'done_with_errors', 'stopped', 'failed'].includes(job.status)) return fail(res, 404, 'report not available yet');
			return send(res, 200, { job_id: job.id, mail: { sent: 900, present: 900, errors: 0 }, calendar: { sent: 120, present: 120, errors: 0 }, contacts: { sent: 80, present: 80, errors: 0 }, sample_check: '20/20 mails read back' });
		}
		publish('job', job);
		return send(res, 200, job);
	}

	return fail(res, 404, 'unknown API route');
}

createServer((req, res) => {
	const url = new URL(req.url, `http://${req.headers.host}`);
	const handle = url.pathname.startsWith('/__mock/') ? control(req, res, url.pathname) : url.pathname.startsWith('/api/') ? api(req, res, url) : Promise.resolve(serveStatic(res, url.pathname));
	Promise.resolve(handle).catch((error) => {
		console.error(error);
		if (!res.headersSent) fail(res, 500, String(error));
	});
}).listen(PORT, '127.0.0.1', () => console.log(`Mock API on http://127.0.0.1:${PORT} (password: admin)`));
