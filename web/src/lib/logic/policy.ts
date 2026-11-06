import type { Policy } from '../api/types.ts';

/** What the policy fields show: text as typed, converted to the API shape by policyBody. */
export interface PolicyForm {
	map: { source: string; target: string }[];
	exclude: string;
	dateFrom: string;
	dateTo: string;
	maxSize: string;
	days: number[];
	start: string;
	end: string;
	tz: string;
	rate: string;
}

export const emptyPolicyForm = (): PolicyForm => ({ map: [], exclude: '', dateFrom: '', dateTo: '', maxSize: '', days: [], start: '', end: '', tz: '', rate: '' });

export function policyToForm(policy: Policy | null | undefined): PolicyForm {
	const p = policy ?? {};
	return {
		map: (p.folder_map ?? []).map((r) => ({ source: r.source, target: r.target })),
		exclude: (p.exclude_folders ?? []).join('\n'),
		dateFrom: p.date_from ?? '',
		dateTo: p.date_to ?? '',
		maxSize: p.max_size_mb != null ? String(p.max_size_mb) : '',
		days: [...(p.window?.days ?? [])],
		start: p.window?.start ?? '',
		end: p.window?.end ?? '',
		tz: p.window?.tz ?? '',
		rate: p.rate_mb_s != null ? String(p.rate_mb_s) : ''
	};
}

const num = (text: string): number | string => {
	const t = text.trim().replace(',', '.');
	if (!t) return '';
	const n = Number(t);
	// A value that is not a number goes to the server as typed, so that the server's own reason is shown.
	return Number.isFinite(n) ? n : t;
};

/**
 * The body for PUT .../policy. Empty fields are left out; an empty policy is `{}`, which clears it.
 * Hours and bandwidth are written for servers only: an account refuses them.
 */
export function policyBody(form: PolicyForm, server: boolean): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	const map = form.map.map((r) => ({ source: r.source.trim(), target: r.target.trim() })).filter((r) => r.source || r.target);
	if (map.length) out.folder_map = map;
	const exclude = form.exclude.split('\n').map((x) => x.trim()).filter(Boolean);
	if (exclude.length) out.exclude_folders = exclude;
	if (form.dateFrom) out.date_from = form.dateFrom;
	if (form.dateTo) out.date_to = form.dateTo;
	const size = num(form.maxSize);
	if (size !== '') out.max_size_mb = size;
	if (server) {
		if (form.start || form.end) {
			const window: Record<string, unknown> = { start: form.start || '00:00', end: form.end || '00:00', tz: form.tz.trim() || 'UTC' };
			if (form.days.length) window.days = [...form.days].sort();
			out.window = window;
		}
		const rate = num(form.rate);
		if (rate !== '') out.rate_mb_s = rate;
	}
	return out;
}

export const policyChanged = (a: PolicyForm, b: PolicyForm): boolean => JSON.stringify(a) !== JSON.stringify(b);

/** Lines "label: value" of an effective policy, in a fixed order. `label` gives the words. */
export function effectiveLines(policy: Policy | undefined, label: (key: string) => string, day: (d: number) => string): { key: string; label: string; value: string }[] {
	if (!policy) return [];
	const lines: { key: string; label: string; value: string }[] = [];
	const add = (key: string, value: string) => lines.push({ key, label: label(key), value });
	if (policy.folder_map?.length) add('map', policy.folder_map.map((r) => `${r.source} > ${r.target}`).join(', '));
	if (policy.exclude_folders?.length) add('exclude', policy.exclude_folders.join(', '));
	if (policy.date_from) add('dateFrom', policy.date_from);
	if (policy.date_to) add('dateTo', policy.date_to);
	if (policy.max_size_mb != null) add('maxSize', String(policy.max_size_mb));
	if (policy.window) {
		const w = policy.window;
		const days = w.days?.length ? `${w.days.map(day).join(' ')} ` : '';
		add('window', `${days}${w.start}-${w.end} (${w.tz})`);
	}
	if (policy.rate_mb_s != null) add('rate', String(policy.rate_mb_s));
	return lines;
}
