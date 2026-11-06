#!/usr/bin/env node
/**
 * Mock API for the UI tests: the HTTP side. The API itself (same contract as app/server/web.py,
 * docs/api.md, fictional data) is web/src/demo/mock-core.mjs, which the static demo runs in the browser too.
 * Serves the build from app/web. Password: admin.
 *   node tests/ui/mock-api.mjs [port]     (npm run mock, from web/)
 * Tests drive it through /__mock/*: reset the state, change delays, push events.
 */
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { control, handle, hooks, subscribe } from '../../web/src/demo/mock-core.mjs';

const PORT = Number(process.argv[2] ?? process.env.PORT ?? 4173);
const WEB = join(dirname(fileURLToPath(import.meta.url)), '../../app/web');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.json': 'application/json' };

const sessions = new Set();
const clients = new Set();

hooks.chunkOk = (bytes, expected) => createHash('sha256').update(bytes).digest('hex') === expected;
subscribe((kind, data) => {
	const text = `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
	for (const res of clients) res.write(text);
});

/** Writes a response of the core: a string is plain text, anything else JSON. */
function write(res, { code, body, headers }) {
	const text = typeof body === 'string' ? body : JSON.stringify(body);
	res.writeHead(code, { 'Content-Type': typeof body === 'string' ? 'text/plain' : 'application/json', ...headers });
	res.end(text);
}
const fail = (res, code, detail) => write(res, { code, body: { detail }, headers: {} });

async function readBody(req) {
	const parts = [];
	for await (const part of req) parts.push(part);
	return Buffer.concat(parts);
}

function serveStatic(res, path) {
	const clean = normalize(path).replace(/^(\.\.[/\\])+/, '');
	let file = join(WEB, clean);
	if (!existsSync(file) || !statSync(file).isFile()) file = join(WEB, 'index.html');
	if (!existsSync(file)) return write(res, { code: 200, body: 'Build missing: run npm run build in web/', headers: {} });
	res.writeHead(200, { 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' });
	createReadStream(file).pipe(res);
}

function authed(req) {
	return (req.headers.cookie ?? '').split(';').some((c) => sessions.has(c.trim().replace('mccb_session=', '')));
}

async function api(req, res, url) {
	const path = url.pathname.slice(4);
	if (path === '/logout') {
		await readBody(req);
		return write(res, { code: 200, body: { ok: true }, headers: { 'Set-Cookie': 'mccb_session=; Path=/; Max-Age=0' } });
	}
	if (path === '/login' && req.method === 'POST') {
		const body = JSON.parse((await readBody(req)).toString() || '{}');
		if (body.password !== 'admin') return fail(res, 401, 'incorrect password');
		const sid = randomUUID();
		sessions.add(sid);
		return write(res, { code: 200, body: { ok: true, authenticated: true }, headers: { 'Set-Cookie': `mccb_session=${sid}; Path=/; HttpOnly; SameSite=Lax` } });
	}
	if (path !== '/health' && path !== '/version' && !authed(req)) {
		await readBody(req);
		return fail(res, 401, 'not authenticated');
	}
	if (path === '/events') {
		res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
		res.write('retry: 3000\n\n');
		clients.add(res);
		req.on('close', () => clients.delete(res));
		return undefined;
	}
	// The body is read once, on demand.
	let buffer;
	const bytes = async () => (buffer ??= await readBody(req));
	const reply = await handle({
		method: req.method,
		path,
		query: url.searchParams,
		headers: req.headers,
		text: async () => (await bytes()).toString(),
		bytes
	});
	// A route that did not read the body still has to drain it.
	await bytes();
	return write(res, reply);
}

createServer((req, res) => {
	const url = new URL(req.url, `http://${req.headers.host}`);
	let work;
	if (url.pathname.startsWith('/__mock/')) {
		work = readBody(req).then(async (raw) => write(res, await control(url.pathname, JSON.parse(raw.toString() || '{}'))));
	} else if (url.pathname.startsWith('/api/')) {
		work = api(req, res, url);
	} else {
		work = Promise.resolve(serveStatic(res, url.pathname));
	}
	work.catch((error) => {
		console.error(error);
		if (!res.headersSent) fail(res, 500, String(error));
	});
}).listen(PORT, '127.0.0.1', () => console.log(`Mock API on http://127.0.0.1:${PORT} (password: admin)`));
