#!/usr/bin/env node
/**
 * Serves web/dist-demo the way the site host will: under /demo/, with the Content-Security-Policy of
 * web/demo-headers, and the nearest 404.html (the app itself) for an unknown path, with a 404 status.
 * There is no API here: a request to /api/ answers 404, so a demo that reached for it would fail the test.
 *   node tests/ui/serve-demo.mjs [port]
 */
import { readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.argv[2] ?? process.env.PORT ?? 4174);
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '../../web');
const DIST = join(ROOT, 'dist-demo');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.json': 'application/json', '.ico': 'image/x-icon' };

/** The policy of the `/demo/*` block of demo-headers, read as the host reads it. */
const CSP = /^\s+Content-Security-Policy:\s*(.+)$/m.exec(readFileSync(join(ROOT, 'demo-headers'), 'utf8'))?.[1];
if (!CSP) throw new Error('demo-headers has no Content-Security-Policy');

function file(path) {
	try {
		return statSync(path).isFile() ? path : null;
	} catch {
		return null;
	}
}

createServer((req, res) => {
	const { pathname } = new URL(req.url, 'http://localhost');
	if (pathname === '/') {
		res.writeHead(302, { Location: '/demo/' });
		return res.end();
	}
	if (!pathname.startsWith('/demo/') && pathname !== '/demo') {
		res.writeHead(404, { 'Content-Type': 'text/plain' });
		return res.end('not found');
	}
	const clean = normalize(pathname.slice('/demo/'.length)).replace(/^(\.\.[/\\])+/, '');
	let target = file(join(DIST, clean)) ?? file(join(DIST, clean, 'index.html'));
	let status = 200;
	if (!target) {
		target = join(DIST, '404.html');
		status = 404;
	}
	const hashed = /\/assets\//.test(pathname);
	res.writeHead(status, {
		'Content-Type': MIME[extname(target)] ?? 'application/octet-stream',
		'Content-Security-Policy': CSP,
		'Cache-Control': hashed ? 'public, max-age=31536000, immutable' : 'no-cache'
	});
	res.end(readFileSync(target));
}).listen(PORT, '127.0.0.1', () => console.log(`Demo on http://127.0.0.1:${PORT}/demo/`));
