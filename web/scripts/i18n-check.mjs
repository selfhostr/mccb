#!/usr/bin/env node
// Fails on a missing or unknown key, a changed {variable}, an em dash or emoji, or a key unknown to the source.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkMessages, checkUsage } from './i18n-lib.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const settings = JSON.parse(readFileSync(join(root, 'project.inlang/settings.json'), 'utf8'));
const locales = {};
const errors = [];
for (const locale of settings.locales) {
	try {
		locales[locale] = JSON.parse(readFileSync(join(root, 'messages', `${locale}.json`), 'utf8'));
	} catch (error) {
		errors.push(`${locale}: ${error.message}`);
	}
}

function walk(dir, out = {}) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) {
			if (entry.name !== 'paraglide') walk(path, out);
		} else if (['.svelte', '.ts'].includes(extname(entry.name))) {
			out[path] = readFileSync(path, 'utf8');
		}
	}
	return out;
}

errors.push(...checkMessages(locales, settings.baseLocale));
if (locales[settings.baseLocale]) errors.push(...checkUsage(walk(join(root, 'src')), locales[settings.baseLocale]));

if (errors.length) {
	for (const error of errors) console.error(error);
	console.error(`i18n check failed: ${errors.length} problem(s)`);
	process.exit(1);
}
console.log(`i18n check ok: ${Object.keys(locales[settings.baseLocale]).length} keys in ${settings.locales.length} locales`);
