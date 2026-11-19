import { describe, expect, it } from 'vitest';
import { decodeText, detectSeparator, type ListColumn, parseMailboxList, splitRecords, templateCsv } from '../../src/lib/logic/mailboxList.ts';

const SERVER: ListColumn[] = [
	{ key: 'source_login', aliases: ['Source login'], required: true, fallback: true },
	{ key: 'source_password', aliases: ['Source password'], required: true, secret: true },
	{ key: 'email', aliases: ['address', 'Destination address'], required: true, email: true },
	{ key: 'password', aliases: ['Destination password'], required: true, secret: true },
	{ key: 'login', aliases: ['Destination login'] }
];
const DEST: ListColumn[] = SERVER.slice(2);

describe('detectSeparator', () => {
	it('finds ; , and tab', () => {
		expect(detectSeparator('a;b;c\nd;e;f')).toBe(';');
		expect(detectSeparator('a,b,c\nd,e,f')).toBe(',');
		expect(detectSeparator('a\tb\tc\nd\te\tf')).toBe('\t');
	});
	it('is not fooled by a separator inside a password or quotes', () => {
		expect(detectSeparator('a;p,w;c\nd;e;f\ng;h;i')).toBe(';');
		expect(detectSeparator('a,"x;y;z",c\nd,e,f')).toBe(',');
	});
	it('falls back to ; for a single column', () => {
		expect(detectSeparator('a@x.example\nb@x.example')).toBe(';');
	});
});

describe('splitRecords', () => {
	it('handles quotes, doubled quotes, separators and line breaks in quotes', () => {
		const recs = splitRecords('"a;b";"say ""hi""";" sp "\n"multi\nline";x', ';');
		expect(recs).toEqual([
			{ line: 1, cells: ['a;b', 'say "hi"', ' sp '] },
			{ line: 2, cells: ['multi\nline', 'x'] }
		]);
	});
	it('drops a BOM, reads Windows line ends and trims unquoted cells', () => {
		expect(splitRecords('﻿ a ; b \r\nc;d\r\n', ';')).toEqual([
			{ line: 1, cells: ['a', 'b'] },
			{ line: 2, cells: ['c', 'd'] }
		]);
	});
	it('counts lines across a quoted line break', () => {
		const recs = splitRecords('"x\r\ny";1\r\nz;2', ';');
		expect(recs.map((r) => r.line)).toEqual([1, 3]);
	});
});

describe('parseMailboxList', () => {
	it('reads rows in the order of the form without a header', () => {
		const r = parseMailboxList('old@a.example;pw1;new@b.example;pw2\nold2@a.example;pw3;new2@b.example;pw4;login2', SERVER);
		expect(r.header).toBe(false);
		expect(r.errors).toEqual([]);
		expect(r.rows.map((x) => x.values)).toEqual([
			{ source_login: 'old@a.example', source_password: 'pw1', email: 'new@b.example', password: 'pw2', login: '' },
			{ source_login: 'old2@a.example', source_password: 'pw3', email: 'new2@b.example', password: 'pw4', login: 'login2' }
		]);
	});

	it('reads a header by name, in any order, with labels or keys', () => {
		const text = 'Destination address,password,Source password,source_login\r\nnew@b.example,pw2,pw1,old\r\n';
		const r = parseMailboxList(text, SERVER);
		expect(r.header).toBe(true);
		expect(r.separator).toBe(',');
		expect(r.rows).toEqual([{ line: 2, values: { source_login: 'old', source_password: 'pw1', email: 'new@b.example', password: 'pw2', login: '' } }]);
	});

	it('reads a tab separated paste from a spreadsheet', () => {
		const r = parseMailboxList('a@b.example\tpw\nc@d.example\tpw', DEST);
		expect(r.rows.map((x) => x.values.email)).toEqual(['a@b.example', 'c@d.example']);
	});

	it('lists the lines it skips, with the reason', () => {
		const text = ['email;password', 'ok@b.example;pw', 'not-an-address;pw', 'missing@b.example', '', '# a comment', 'OK@b.example;pw', 'taken@b.example;pw'].join('\n');
		const r = parseMailboxList(text, DEST, ['taken@b.example']);
		expect(r.rows.map((x) => x.values.email)).toEqual(['ok@b.example']);
		expect(r.errors).toEqual([
			{ line: 3, reason: 'email', column: 'email' },
			{ line: 4, reason: 'missing', column: 'password' },
			{ line: 7, reason: 'duplicate', column: 'email' },
			{ line: 8, reason: 'duplicate', column: 'email' }
		]);
	});

	it('takes the source login as the address when the address is empty', () => {
		const r = parseMailboxList('same@b.example;pw1;;pw2', SERVER);
		expect(r.errors).toEqual([]);
		expect(r.rows[0].values.email).toBe('');
		const bad = parseMailboxList('jdoe;pw1;;pw2', SERVER);
		expect(bad.errors).toEqual([{ line: 1, reason: 'missing', column: 'email' }]);
	});

	it('reports a required column absent from the header on every row', () => {
		const r = parseMailboxList('email\na@b.example\nc@d.example', DEST);
		expect(r.rows).toEqual([]);
		expect(r.errors.map((e) => [e.line, e.reason, e.column])).toEqual([
			[2, 'missing', 'password'],
			[3, 'missing', 'password']
		]);
	});

	it('keeps a quoted password with separators and spaces', () => {
		const r = parseMailboxList('a@b.example;" p;w ""x"" "', DEST);
		expect(r.rows[0].values.password).toBe(' p;w "x" ');
	});

	it('reads nothing from an empty text', () => {
		expect(parseMailboxList('﻿\r\n\r\n', DEST)).toEqual({ rows: [], errors: [], header: false, separator: ';' });
	});
});

describe('templateCsv', () => {
	it('writes exactly the given columns and reads back', () => {
		const csv = templateCsv(DEST.slice(0, 2), [{ email: 'a@b.example', password: 'p;w' }]);
		expect(csv).toBe('email;password\r\na@b.example;"p;w"\r\n');
		const r = parseMailboxList(csv, DEST);
		expect(r.header).toBe(true);
		expect(r.rows[0].values).toEqual({ email: 'a@b.example', password: 'p;w', login: '' });
	});
	it('writes one row of the column samples by default', () => {
		expect(templateCsv([{ key: 'email', sample: 'a@b.example' }, { key: 'password', sample: 'secret' }])).toBe('email;password\r\na@b.example;secret\r\n');
	});
});

describe('decodeText', () => {
	it('reads UTF-8 and falls back to Windows-1252', () => {
		expect(decodeText(new TextEncoder().encode('é;ü'))).toBe('é;ü');
		expect(decodeText(new Uint8Array([0xe9, 0x3b, 0x61]))).toBe('é;a');
	});
});
