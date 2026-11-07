import AtSign from '@lucide/svelte/icons/at-sign';
import Building2 from '@lucide/svelte/icons/building-2';
import Calendar from '@lucide/svelte/icons/calendar';
import Cloud from '@lucide/svelte/icons/cloud';
import Contact from '@lucide/svelte/icons/contact';
import FileText from '@lucide/svelte/icons/file-text';
import Files from '@lucide/svelte/icons/files';
import Globe from '@lucide/svelte/icons/globe';
import Mail from '@lucide/svelte/icons/mail';
import Plug from '@lucide/svelte/icons/plug';
import Zap from '@lucide/svelte/icons/zap';
import type { Component } from 'svelte';
import type { ServerInput } from '../api/types.ts';

type Icon = Component<{ size?: number }>;

/**
 * Mail providers a destination server can be picked from. A preset only fills the form: nothing is
 * stored about the provider, the host says it again when the list is drawn.
 */
export interface Provider {
	id: string;
	name: string;
	/** A neutral icon: the app carries no brand colours. */
	icon: Icon;
	/** Hosts that are this provider, to draw the mark of a saved server. */
	hosts: RegExp | null;
	preset: Partial<ServerInput>;
	/** Shown under the picker when chosen: what the provider asks for. */
	note: 'app_password' | 'oauth' | null;
}

export const PROVIDERS: Provider[] = [
	{ id: 'imap', name: 'IMAP', icon: AtSign, hosts: null, preset: { imap_host: '', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: null },
	{ id: 'gmail', name: 'Gmail', icon: Mail, hosts: /(^|\.)gmail\.com$|googlemail\.com$/i, preset: { name: 'Gmail', imap_host: 'imap.gmail.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: 'app_password' },
	{ id: 'microsoft', name: 'Microsoft 365', icon: Cloud, hosts: /office365\.com$|outlook\.com$/i, preset: { name: 'Microsoft 365', imap_host: 'outlook.office365.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: 'oauth' },
	{ id: 'icloud', name: 'iCloud', icon: Cloud, hosts: /(^|\.)mail\.me\.com$|icloud\.com$/i, preset: { name: 'iCloud', imap_host: 'imap.mail.me.com', imap_port: 993, imap_security: 'ssl', dav_enabled: true, caldav_url: 'https://caldav.icloud.com/', carddav_url: 'https://contacts.icloud.com/' }, note: 'app_password' },
	{ id: 'fastmail', name: 'Fastmail', icon: Zap, hosts: /fastmail\.com$/i, preset: { name: 'Fastmail', imap_host: 'imap.fastmail.com', imap_port: 993, imap_security: 'ssl', dav_enabled: true, caldav_url: 'https://caldav.fastmail.com/dav/calendars/user/{email}/', carddav_url: 'https://carddav.fastmail.com/dav/addressbooks/user/{email}/Default/' }, note: 'app_password' },
	{ id: 'infomaniak', name: 'Infomaniak', icon: Globe, hosts: /infomaniak\.(com|ch)$/i, preset: { name: 'Infomaniak', imap_host: 'mail.infomaniak.com', imap_port: 993, imap_security: 'ssl', dav_enabled: false }, note: null }
];

export function providerOf(host: string | null | undefined): Provider {
	const h = (host ?? '').trim();
	return PROVIDERS.find((p) => p.hosts && p.hosts.test(h)) ?? PROVIDERS[0];
}

/** The icon of a connector kind, for the source and destination pickers. Unknown kinds get a plug. */
export function kindIcon(kind: string): Icon {
	const known: Record<string, Icon> = {
		pst: FileText,
		files: Files,
		imap: AtSign,
		imap_oauth2: AtSign,
		exchange_ews: Building2,
		m365_graph: Cloud,
		google: Mail,
		jmap: Zap,
		caldav: Calendar,
		carddav: Contact
	};
	return known[kind] ?? Plug;
}
