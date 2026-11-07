import type { ServerInput } from '../api/types.ts';

/**
 * Mail providers a destination server can be picked from. A preset only fills the form: nothing is
 * stored about the provider, the host says it again when the list is drawn.
 */
export interface Provider {
	id: string;
	name: string;
	/** Two letters drawn on the mark, and its colour. */
	mono: string;
	tone: string;
	/** Hosts that are this provider, to draw the mark of a saved server. */
	hosts: RegExp | null;
	preset: Partial<ServerInput>;
	/** Shown under the picker when chosen: what the provider asks for. */
	note: 'app_password' | 'oauth' | null;
}

export const PROVIDERS: Provider[] = [
	{ id: 'imap', name: 'IMAP', mono: 'IM', tone: 'var(--c-mark-neutral)', hosts: null, preset: { imap_host: '', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: null },
	{ id: 'gmail', name: 'Gmail', mono: 'G', tone: 'var(--c-mark-google)', hosts: /(^|\.)gmail\.com$|googlemail\.com$/i, preset: { name: 'Gmail', imap_host: 'imap.gmail.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: 'app_password' },
	{ id: 'microsoft', name: 'Microsoft 365', mono: 'MS', tone: 'var(--c-mark-microsoft)', hosts: /office365\.com$|outlook\.com$/i, preset: { name: 'Microsoft 365', imap_host: 'outlook.office365.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: 'oauth' },
	{ id: 'icloud', name: 'iCloud', mono: 'iC', tone: 'var(--c-mark-icloud)', hosts: /(^|\.)mail\.me\.com$|icloud\.com$/i, preset: { name: 'iCloud', imap_host: 'imap.mail.me.com', imap_port: 993, imap_security: 'ssl', dav_enabled: true, caldav_url: 'https://caldav.icloud.com/', carddav_url: 'https://contacts.icloud.com/' }, note: 'app_password' },
	{ id: 'fastmail', name: 'Fastmail', mono: 'FM', tone: 'var(--c-mark-fastmail)', hosts: /fastmail\.com$/i, preset: { name: 'Fastmail', imap_host: 'imap.fastmail.com', imap_port: 993, imap_security: 'ssl', dav_enabled: true, caldav_url: 'https://caldav.fastmail.com/dav/calendars/user/{email}/', carddav_url: 'https://carddav.fastmail.com/dav/addressbooks/user/{email}/Default/' }, note: 'app_password' },
	{ id: 'infomaniak', name: 'Infomaniak', mono: 'IK', tone: 'var(--c-mark-infomaniak)', hosts: /infomaniak\.(com|ch)$/i, preset: { name: 'Infomaniak', imap_host: 'mail.infomaniak.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: null }
];

export function providerOf(host: string | null | undefined): Provider {
	const h = (host ?? '').trim();
	return PROVIDERS.find((p) => p.hosts && p.hosts.test(h)) ?? PROVIDERS[0];
}

/** The mark of a connector kind, for the source and destination pickers. Unknown kinds get a neutral one. */
export function kindMark(kind: string, label: string): { mono: string; tone: string } {
	const known: Record<string, { mono: string; tone: string }> = {
		pst: { mono: 'PST', tone: 'var(--c-mark-pst)' },
		files: { mono: 'FILE', tone: 'var(--c-mark-file)' },
		imap: { mono: 'IM', tone: 'var(--c-mark-neutral)' },
		imap_oauth2: { mono: 'IM', tone: 'var(--c-mark-neutral)' },
		exchange_ews: { mono: 'EX', tone: 'var(--c-mark-microsoft)' },
		m365_graph: { mono: 'MS', tone: 'var(--c-mark-microsoft)' },
		google: { mono: 'G', tone: 'var(--c-mark-google)' },
		jmap: { mono: 'JM', tone: 'var(--c-mark-jmap)' },
		caldav: { mono: 'CAL', tone: 'var(--c-mark-dav)' },
		carddav: { mono: 'CRD', tone: 'var(--c-mark-dav)' }
	};
	return known[kind] ?? { mono: (label || kind).slice(0, 2).toUpperCase(), tone: 'var(--c-mark-neutral)' };
}
