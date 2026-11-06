import { maskEmail, maskFile, maskHost, maskText } from './logic/mask.ts';

export interface Prefs {
	theme: 'dark' | 'light';
	density: 'compact' | 'comfortable';
	maskAddresses: boolean;
	uploadConcurrency: number;
}

const KEY = 'mccb.prefs.v1';
const DEFAULTS: Prefs = {
	theme: 'dark',
	density: 'compact',
	maskAddresses: false,
	uploadConcurrency: 1
};

function load(): Prefs {
	try {
		const saved = JSON.parse(localStorage.getItem(KEY) ?? '{}');
		return { ...DEFAULTS, ...saved };
	} catch {
		return { ...DEFAULTS };
	}
}

class PrefStore {
	value = $state.raw<Prefs>(load());

	set<K extends keyof Prefs>(key: K, value: Prefs[K]): void {
		this.value = { ...this.value, [key]: value };
		try {
			localStorage.setItem(KEY, JSON.stringify(this.value));
		} catch {
			// Storage blocked: the choice lasts until the page closes.
		}
		this.apply();
	}

	apply(): void {
		const root = document.documentElement;
		root.dataset.theme = this.value.theme;
		root.dataset.density = this.value.density;
	}

	email = (v: string | null | undefined): string => (v ? (this.value.maskAddresses ? maskEmail(v) : v) : '');
	host = (v: string | null | undefined): string => (v ? (this.value.maskAddresses ? maskHost(v) : v) : '');
	file = (v: string | null | undefined): string => (v ? (this.value.maskAddresses ? maskFile(v) : v) : '');
	text = (v: string | null | undefined): string => (v ? (this.value.maskAddresses ? maskText(v) : v) : '');
}

export const prefs = new PrefStore();
