import { getLocale, type Locale, locales, setLocale } from '#lib/paraglide/runtime.js';
import { setFormatLocale } from './format.ts';

/** Every language by its own name. Names are not translated. */
export const LANGUAGE_NAMES: Record<string, string> = {
	en: 'English',
	fr: 'Français',
	de: 'Deutsch',
	es: 'Español',
	it: 'Italiano',
	pt: 'Português',
	'pt-BR': 'Português (Brasil)',
	ru: 'Русский',
	'zh-Hans': '简体中文'
};

export const availableLocales: readonly string[] = locales;

class I18n {
	/** Read by the root {#key}: changing language rebuilds the tree without a reload, so uploads keep running. */
	locale = $state<string>(getLocale());

	constructor() {
		this.sync();
	}

	change(next: string): void {
		void setLocale(next as Locale, { reload: false });
		this.locale = next;
		this.sync();
	}

	private sync(): void {
		document.documentElement.lang = this.locale;
		setFormatLocale(this.locale);
	}
}

export const i18n = new I18n();
