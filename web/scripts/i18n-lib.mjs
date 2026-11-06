/** Pure checks on the message files, shared by scripts/i18n-check.mjs and the unit tests. */

const VARIABLE = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
// Em dash and emoji: none of them in UI copy.
const BANNED = /[\u2014\p{Extended_Pictographic}]/u;

export function variablesOf(text) {
	return [...text.matchAll(VARIABLE)].map((m) => m[1]).sort();
}

/** @param {Record<string, Record<string, string>>} locales `{ en: {...}, fr: {...} }` */
export function checkMessages(locales, base = 'en') {
	const errors = [];
	const source = locales[base];
	if (!source) return [`base locale "${base}" is missing`];
	for (const [locale, messages] of Object.entries(locales)) {
		for (const key of Object.keys(source)) {
			if (!(key in messages)) {
				errors.push(`${locale}: missing key "${key}"`);
				continue;
			}
			if (typeof messages[key] !== 'string' || !messages[key].trim()) {
				errors.push(`${locale}: empty message "${key}"`);
				continue;
			}
			const want = variablesOf(source[key]).join(',');
			const got = variablesOf(messages[key]).join(',');
			if (want !== got) errors.push(`${locale}: "${key}" has variables {${got}} instead of {${want}}`);
			if (BANNED.test(messages[key])) errors.push(`${locale}: "${key}" has an em dash or an emoji`);
		}
		for (const key of Object.keys(messages)) {
			if (key !== '$schema' && !(key in source)) errors.push(`${locale}: unknown key "${key}"`);
		}
	}
	return errors;
}

/** Keys used by the source files: every `m.key(` must exist in the base locale, with the right variables. */
export function checkUsage(sources, baseMessages) {
	const errors = [];
	const used = new Set();
	for (const [file, text] of Object.entries(sources)) {
		for (const match of text.matchAll(/\bm\.([a-zA-Z0-9_]+)\(/g)) {
			used.add(match[1]);
			if (!(match[1] in baseMessages)) errors.push(`${file}: unknown message "${match[1]}"`);
		}
	}
	for (const key of Object.keys(baseMessages)) {
		if (key !== '$schema' && !used.has(key)) errors.push(`unused message "${key}"`);
	}
	return errors;
}
