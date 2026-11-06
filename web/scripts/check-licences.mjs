#!/usr/bin/env node
// Every installed package must carry an OSI-approved licence. Reads node_modules, so run it after `npm ci`.
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const OSI = new Set(['MIT', 'MIT-0', 'ISC', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause', '0BSD', 'OFL-1.1', 'MPL-2.0', 'Python-2.0', 'BlueOak-1.0.0', 'Unlicense', 'Zlib']);

function allowed(licence) {
	if (!licence) return false;
	// "(MIT OR Apache-2.0)": one choice is enough.
	return String(licence)
		.replace(/[()]/g, '')
		.split(/\s+(?:OR|AND)\s+/)
		.some((part) => OSI.has(part.trim()));
}

const root = new URL('../node_modules/', import.meta.url).pathname;
const bad = [];
function scan(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (!entry.isDirectory() || entry.name === '.bin' || entry.name === '.cache') continue;
		const path = join(dir, entry.name);
		if (entry.name.startsWith('@')) {
			scan(path);
			continue;
		}
		const manifest = join(path, 'package.json');
		if (existsSync(manifest)) {
			const pkg = JSON.parse(readFileSync(manifest, 'utf8'));
			const licence = pkg.license ?? pkg.licenses?.[0]?.type;
			// A few packages only ship a LICENSE file: those are read by hand and listed here.
			if (!allowed(licence) && pkg.name !== '@inlang/plugin-message-format') bad.push(`${pkg.name}@${pkg.version}: ${licence ?? 'no licence field'}`);
		}
		if (existsSync(join(path, 'node_modules'))) scan(join(path, 'node_modules'));
	}
}
scan(root);

if (bad.length) {
	for (const line of bad) console.error(line);
	process.exit(1);
}
console.log('licences ok');
