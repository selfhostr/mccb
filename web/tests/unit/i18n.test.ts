import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { checkMessages, checkUsage, variablesOf } from '../../scripts/i18n-lib.mjs';

const root = fileURLToPath(new URL('../..', import.meta.url));

describe('i18n check', () => {
	const en = { a: 'Hello {name}', b: 'Bye' };

	it('passes when every locale matches', () => {
		expect(checkMessages({ en, fr: { a: 'Salut {name}', b: 'Adieu' } })).toEqual([]);
	});

	it('fails on a missing key', () => {
		expect(checkMessages({ en, fr: { a: 'Salut {name}' } })).toEqual(['fr: missing key "b"']);
	});

	it('fails on an unknown key', () => {
		expect(checkMessages({ en, fr: { a: 'Salut {name}', b: 'x', c: 'y' } })).toEqual(['fr: unknown key "c"']);
	});

	it('fails on a changed variable', () => {
		const errors = checkMessages({ en, fr: { a: 'Salut {nom}', b: 'x' } });
		expect(errors).toHaveLength(1);
		expect(errors[0]).toContain('{nom}');
	});

	it('fails on an empty message', () => {
		expect(checkMessages({ en, fr: { a: 'Salut {name}', b: ' ' } })).toEqual(['fr: empty message "b"']);
	});

	it('fails on an em dash or an emoji', () => {
		expect(checkMessages({ en: { a: 'one \u2014 two' } })).toHaveLength(1);
		expect(checkMessages({ en: { a: 'nice \u{1F680}' } })).toHaveLength(1);
	});

	it('reads variables', () => {
		expect(variablesOf('{b} and {a}')).toEqual(['a', 'b']);
	});

	it('checks usage in both directions', () => {
		expect(checkUsage({ 'x.svelte': 'm.a() m.c()' }, { a: '', b: '' })).toEqual(['x.svelte: unknown message "c"', 'unused message "b"']);
	});

	it('the real files pass', () => {
		const settings = JSON.parse(readFileSync(join(root, 'project.inlang/settings.json'), 'utf8'));
		const locales: Record<string, Record<string, string>> = {};
		for (const locale of settings.locales) locales[locale] = JSON.parse(readFileSync(join(root, 'messages', `${locale}.json`), 'utf8'));
		expect(settings.locales).toHaveLength(9);
		expect(checkMessages(locales)).toEqual([]);
	});
});

function files(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory() && entry.name !== 'paraglide') files(path, out);
		else if (/\.(svelte|ts)$/.test(entry.name)) out.push(path);
	}
	return out;
}

describe('tokens', () => {
	it('no hex colour outside tokens.css, no Tailwind default palette', () => {
		const hits: string[] = [];
		for (const path of files(join(root, 'src'))) {
			const text = readFileSync(path, 'utf8');
			if (/(?:^|[\s"'`:(])#[0-9a-fA-F]{3,8}\b/.test(text)) hits.push(path);
		}
		expect(hits).toEqual([]);
	});

	const css = readFileSync(join(root, 'src/tokens.css'), 'utf8');
	const block = (selector: string) => {
		const start = css.indexOf(selector);
		const body = css.slice(start, css.indexOf('}', start));
		return Object.fromEntries([...body.matchAll(/--(c-[\w-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
	};
	const luminance = (hex: string) => {
		const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const contrast = (a: string, b: string) => {
		const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
		return (x + 0.05) / (y + 0.05);
	};

	it.each([
		['dark', ":root,\nhtml[data-theme='dark']"],
		['light', "html[data-theme='light']"]
	])('text pairs reach 4.5:1 on the %s theme', (_name, selector) => {
		const t = block(selector);
		for (const ground of ['c-canvas', 'c-surface', 'c-surface-2']) {
			for (const ink of ['c-ink', 'c-ink-2', 'c-ink-3', 'c-signal-ink', 'c-advisory-ink', 'c-warning-ink', 'c-info-ink']) {
				expect(contrast(t[ink], t[ground]), `${ink} on ${ground}`).toBeGreaterThanOrEqual(4.5);
			}
		}
		expect(contrast(t['c-on-primary'], t['c-primary'])).toBeGreaterThanOrEqual(4.5);
	});

	it('both themes define the same names', () => {
		const names = (selector: string) => {
			const start = css.indexOf(selector);
			return [...css.slice(start, css.indexOf('}', start)).matchAll(/--([\w-]+):/g)].map((m) => m[1]).sort();
		};
		expect(names("html[data-theme='light']")).toEqual(names(":root,\nhtml[data-theme='dark']"));
	});
});
