/** Formatting through Intl, in the active language. `setFormatLocale` is called when the language changes. */
let locale: string | undefined;

let numbers: Intl.NumberFormat | undefined;
let dates: Intl.DateTimeFormat | undefined;

export function setFormatLocale(value: string | undefined): void {
	locale = value;
	numbers = undefined;
	dates = undefined;
}

export const DASH = '-';

export function fmtNum(n: number | null | undefined): string {
	return n == null ? DASH : (numbers ??= new Intl.NumberFormat(locale)).format(n);
}

const UNITS = ['byte', 'kilobyte', 'megabyte', 'gigabyte', 'terabyte'];

export function fmtBytes(n: number | null | undefined): string {
	if (n == null || Number.isNaN(n)) return DASH;
	let value = n;
	let i = 0;
	while (value >= 1024 && i < UNITS.length - 1) {
		value /= 1024;
		i++;
	}
	const digits = i === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
	return new Intl.NumberFormat(locale, { style: 'unit', unit: UNITS[i], maximumFractionDigits: digits }).format(value);
}

export function fmtDuration(seconds: number | null | undefined): string {
	if (seconds == null || !Number.isFinite(seconds)) return DASH;
	const unit = (u: string, v: number) => new Intl.NumberFormat(locale, { style: 'unit', unit: u, unitDisplay: 'narrow' }).format(v);
	const s = Math.round(seconds);
	if (s < 60) return unit('second', s);
	const min = Math.floor(s / 60);
	if (min < 60) return `${unit('minute', min)} ${unit('second', s % 60)}`;
	return `${unit('hour', Math.floor(min / 60))} ${unit('minute', min % 60)}`;
}

export function pct(done: number, total: number): number {
	return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
}

/** The server sends epoch seconds, or ISO text in UTC without a suffix. */
export function toDate(value: number | string | null | undefined): Date | null {
	if (value == null || value === '') return null;
	if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value);
	const text = /(?:Z|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value}Z`;
	const date = new Date(text);
	return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtAbsolute(date: Date | null): string {
	return date ? (dates ??= new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'medium' })).format(date) : DASH;
}

const STEPS: [number, Intl.RelativeTimeFormatUnit][] = [
	[60, 'second'],
	[3600, 'minute'],
	[86400, 'hour'],
	[604800, 'day']
];

/** Relative up to a week, then the date. */
export function fmtRelative(date: Date | null, now: number): string {
	if (!date) return DASH;
	const diff = (date.getTime() - now) / 1000;
	const abs = Math.abs(diff);
	const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
	let span = 1;
	for (const [limit, unit] of STEPS) {
		if (abs < limit) return rtf.format(Math.round(diff / span), unit);
		span = limit;
	}
	return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(date);
}
