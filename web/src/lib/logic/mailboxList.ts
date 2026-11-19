/**
 * A list of mailboxes, pasted or read from a .csv/.txt file, turned into rows for the new migration form.
 * The columns follow the form: the source fields first, then the destination address, password and login.
 * The separator (; , or tab) is found from the text, a quoted value may hold a separator, a quote or a line
 * break, a BOM and Windows line ends are ignored. A first line naming the columns is a header: the columns are
 * then read by name, in any order; without one they are read in the order of the form.
 */

export interface ListColumn {
	/** Stable name: the header the template writes. */
	key: string;
	/** Other names a header may use for it: the label shown in the form, common spellings. */
	aliases?: string[];
	/** A row without it is skipped. */
	required?: boolean;
	/** The destination address: checked, and unique in the list. */
	email?: boolean;
	/** A value with an @ here stands for the address when the address column is empty. */
	fallback?: boolean;
	secret?: boolean;
	/** The value the template shows for it. */
	sample?: string;
}

export type ListErrorReason = 'email' | 'missing' | 'duplicate';

export interface ListError {
	/** 1-based line of the file where the record starts. */
	line: number;
	reason: ListErrorReason;
	/** The column at fault, by key. */
	column?: string;
}

export interface ListRow {
	line: number;
	values: Record<string, string>;
}

export interface ListResult {
	rows: ListRow[];
	errors: ListError[];
	header: boolean;
	separator: Separator;
}

export type Separator = ';' | ',' | '\t';
const SEPARATORS: Separator[] = ['\t', ';', ','];

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const isEmail = (v: string) => EMAIL.test(v);

/** Lowercase, without accents, spaces, dashes or underscores: "Mot de passe" and "mot_de_passe" match. */
export const normalize = (v: string) =>
	v
		.normalize('NFD')
		.replace(/\p{M}/gu, '')
		.toLowerCase()
		.replace(/[\s_\-.]/g, '');

/** Counts a character outside quotes. */
function countOutside(line: string, ch: string): number {
	let n = 0;
	let quoted = false;
	for (const c of line) {
		if (c === '"') quoted = !quoted;
		else if (c === ch && !quoted) n++;
	}
	return n;
}

/** The separator used by most of the first lines, the same number of times on each when it can. */
export function detectSeparator(text: string): Separator {
	const lines = stripBom(text)
		.split(/\r\n|\n|\r/)
		.filter((l) => l.trim() && !l.trimStart().startsWith('#'))
		.slice(0, 20);
	let best: Separator = ';';
	let bestScore = -1;
	for (const sep of SEPARATORS) {
		const counts = lines.map((l) => countOutside(l, sep));
		const present = counts.filter((c) => c > 0).length;
		if (!present) continue;
		const steady = counts.every((c) => c === counts[0]) ? 1 : 0;
		const score = present * 2 + steady;
		if (score > bestScore) {
			best = sep;
			bestScore = score;
		}
	}
	return best;
}

const stripBom = (text: string) => (text.charCodeAt(0) === 0xfeff ? text.slice(1) : text);

/** Splits the text into records of cells. A quoted cell keeps its spaces; an unquoted one is trimmed. */
export function splitRecords(text: string, sep: Separator): { line: number; cells: string[] }[] {
	const src = stripBom(text);
	const out: { line: number; cells: string[] }[] = [];
	let cells: string[] = [];
	let cell = '';
	let quoted = false;
	let wasQuoted = false;
	let line = 1;
	let start = 1;
	const endCell = () => {
		cells.push(wasQuoted ? cell : cell.trim());
		cell = '';
		wasQuoted = false;
	};
	const endRecord = () => {
		endCell();
		out.push({ line: start, cells });
		cells = [];
	};
	for (let i = 0; i < src.length; i++) {
		const c = src[i];
		if (quoted) {
			if (c === '"') {
				if (src[i + 1] === '"') {
					cell += '"';
					i++;
				} else quoted = false;
			} else {
				if (c === '\n' || (c === '\r' && src[i + 1] !== '\n')) line++;
				cell += c;
			}
			continue;
		}
		if (c === '"' && !cell.trim()) {
			quoted = true;
			wasQuoted = true;
			cell = '';
		} else if (c === sep) endCell();
		else if (c === '\r' || c === '\n') {
			if (c === '\r' && src[i + 1] === '\n') i++;
			endRecord();
			line++;
			start = line;
		} else if (!(wasQuoted && !quoted)) cell += c;
		// Text after a closing quote ("a"b) is dropped: the quoted part is the value.
	}
	if (cell || cells.length || wasQuoted) endRecord();
	return out;
}

const names = (c: ListColumn) => [c.key, ...(c.aliases ?? [])].map(normalize).filter(Boolean);

/** The column index of each column when the record is a header, null when it is data. */
function headerMap(cells: string[], columns: ListColumn[]): Map<string, number> | null {
	if (cells.some((v) => v.includes('@'))) return null;
	const map = new Map<string, number>();
	cells.forEach((cell, i) => {
		const n = normalize(cell);
		if (!n) return;
		// The exact key wins over an alias: "login" is the destination login, not a source alias.
		const col = columns.find((c) => normalize(c.key) === n && !map.has(c.key)) ?? columns.find((c) => !map.has(c.key) && names(c).includes(n));
		if (col) map.set(col.key, i);
	});
	return map.size ? map : null;
}

/**
 * Reads the list. `taken` holds the addresses already in the form or on the destination server, lowercased:
 * a row that repeats one is a duplicate. Blank lines and lines starting with # are left out without a word.
 */
export function parseMailboxList(text: string, columns: ListColumn[], taken: Iterable<string> = []): ListResult {
	const separator = detectSeparator(text);
	const records = splitRecords(text, separator).filter((r) => r.cells.some((c) => c) && !r.cells[0].trimStart().startsWith('#'));
	const head = records.length ? headerMap(records[0].cells, columns) : null;
	if (head) records.shift();
	const seen = new Set([...taken].map((v) => v.toLowerCase()));
	const emailCol = columns.find((c) => c.email);
	const rows: ListRow[] = [];
	const errors: ListError[] = [];
	for (const rec of records) {
		const values: Record<string, string> = {};
		columns.forEach((c, i) => {
			const at = head ? head.get(c.key) : i;
			values[c.key] = at === undefined ? '' : (rec.cells[at] ?? '');
		});
		const missing = columns.find((c) => c.required && !values[c.key] && !(c.email && fallbackOf(values, columns)));
		if (missing) {
			errors.push({ line: rec.line, reason: 'missing', column: missing.key });
			continue;
		}
		if (emailCol) {
			const address = (values[emailCol.key] || fallbackOf(values, columns)).trim();
			if (!isEmail(address)) {
				errors.push({ line: rec.line, reason: 'email', column: emailCol.key });
				continue;
			}
			const key = address.toLowerCase();
			if (seen.has(key)) {
				errors.push({ line: rec.line, reason: 'duplicate', column: emailCol.key });
				continue;
			}
			seen.add(key);
		}
		rows.push({ line: rec.line, values });
	}
	return { rows, errors, header: !!head, separator };
}

function fallbackOf(values: Record<string, string>, columns: ListColumn[]): string {
	return columns.filter((c) => c.fallback && !c.secret).map((c) => values[c.key]?.trim() ?? '').find((v) => v.includes('@')) ?? '';
}

/** The text of a file: UTF-8 when it is valid UTF-8, else Windows-1252 (what Excel writes by default). */
export function decodeText(bytes: ArrayBuffer | Uint8Array): string {
	try {
		return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
	} catch {
		return new TextDecoder('windows-1252').decode(bytes);
	}
}

/** One cell of a CSV written with `sep`: quoted when it holds the separator, a quote or a line break. */
const quote = (v: string, sep: Separator) => (v.includes(sep) || v.includes('"') || /[\r\n]/.test(v) ? `"${v.replaceAll('"', '""')}"` : v);

/** A CSV with exactly these columns: the header, then the sample rows (by key), by default one row of the column samples. */
export function templateCsv(columns: ListColumn[], samples: Record<string, string>[] = [Object.fromEntries(columns.map((c) => [c.key, c.sample ?? '']))], sep: Separator = ';'): string {
	const lines = [columns.map((c) => c.key), ...samples.map((s) => columns.map((c) => s[c.key] ?? ''))];
	return lines.map((cells) => cells.map((v) => quote(v, sep)).join(sep)).join('\r\n') + '\r\n';
}
