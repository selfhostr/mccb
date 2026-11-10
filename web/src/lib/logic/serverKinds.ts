/**
 * Servers that are not plain IMAP rows: a saved connection of a connector kind (JMAP, Exchange, Google...).
 * Two things only the UI decides: which settings are typed once on the server, and which ones each mailbox
 * gives. The API accepts any split: what the server holds wins, a mailbox stores the rest.
 */
import type { Connector, ConnectorField, ConnectorRole, SourceConnection } from '../api/types.ts';

type Config = Record<string, unknown>;

/** A kind with a directory (a Microsoft 365 tenant) lists its mailboxes itself: it has its own screens. */
export const isDirectory = (catalogue: readonly Connector[], kind: string): boolean => catalogue.find((c) => c.kind === kind)?.directory === true;

const usesServiceAccount = (config: Config) => !!config.access && config.access !== 'own';

/** Settings typed once on the server, in the order of the form. */
const SHARED: Record<string, (config: Config) => string[]> = {
	jmap: () => ['url', 'auth', 'verify_tls'],
	exchange_ews: (c) => ['url', 'auth', 'server_version', 'verify_tls', 'access', ...(usesServiceAccount(c) ? ['login', 'password'] : []), 'include_archive'],
	imap_oauth2: () => ['host', 'port', 'security', 'verify_tls', 'token_url', 'client_id', 'client_secret', 'scope'],
	google: (c) => ['auth', ...(c.auth === 'oauth' ? ['client_id', 'client_secret'] : ['service_account_key']), 'sync_mail', 'sync_calendar', 'sync_contacts', 'include_trash_spam'],
	imap: () => ['host', 'port', 'security', 'verify_tls'],
	caldav: () => ['url', 'verify_tls'],
	carddav: () => ['url', 'verify_tls']
};

/** What one mailbox gives on such a server, in the order of the columns. */
const PER_MAILBOX: Record<string, (config: Config) => string[]> = {
	jmap: (c) => (c.auth === 'bearer' ? ['password'] : ['login', 'password']),
	exchange_ews: (c) => (usesServiceAccount(c) ? ['mailbox'] : ['login', 'password']),
	imap_oauth2: () => ['login', 'refresh_token'],
	google: (c) => (c.auth === 'oauth' ? ['user', 'refresh_token'] : ['user']),
	imap: () => ['login', 'password'],
	caldav: () => ['login', 'password'],
	carddav: () => ['login', 'password']
};

const pick = (role: ConnectorRole, names: string[]): ConnectorField[] => names.flatMap((name) => role.fields.find((f) => f.name === name) ?? []);

/** The role of the server form: only the shared settings; what the form does not show is not asked. */
export function sharedRole(role: ConnectorRole, kind: string, config: Config): ConnectorRole {
	const names = SHARED[kind]?.(config);
	return { ...role, fields: names ? pick(role, names) : role.fields.filter((f) => !f.secret).map((f) => ({ ...f, required: false })) };
}

/** The fields one mailbox still has to give on this server: never one the server already holds. */
export function mailboxFields(role: ConnectorRole, connection: Pick<SourceConnection, 'kind' | 'config' | 'secret_names'>): ConnectorField[] {
	const held = new Set([...Object.keys(connection.config), ...(connection.secret_names ?? [])]);
	const names = PER_MAILBOX[connection.kind]?.(connection.config) ?? role.fields.filter((f) => f.required).map((f) => f.name);
	return pick(role, names).filter((f) => !held.has(f.name));
}

/** The line under a server's name: where it is. */
export function connectionAddress(config: Config): string {
	for (const key of ['tenant_id', 'url', 'host']) {
		const value = config[key];
		if (typeof value === 'string' && value) return value;
	}
	return '';
}

/** What the way in knows about the server being added: its name, first values and what to do first. */
export interface ServerPreset {
	title?: string;
	name?: string;
	config?: Record<string, unknown>;
	note?: string;
	link?: { label: string; href: string };
}
