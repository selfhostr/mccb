/** Mirrors app/server/repo.py. */
export interface Server {
	id: number;
	name: string;
	imap_host: string;
	imap_port: number;
	imap_security: 'ssl' | 'starttls' | 'none';
	verify_tls: boolean;
	root_folder: string | null;
	caldav_url: string | null;
	carddav_url: string | null;
	dav_enabled: boolean;
	workers: number;
	notes: string;
	tuning: 'auto' | 'manual';
	builders: number | null;
}

export type ServerInput = Omit<Server, 'id'>;

export interface ProbeResult {
	ok: boolean;
	msg: string;
	quota?: unknown;
}

export interface TestResult {
	imap?: ProbeResult;
	caldav?: ProbeResult;
	carddav?: ProbeResult;
}

export type AccountStatus = 'new' | 'ok' | 'auth_failed' | 'error';
export interface AccountOptions {
	mail: boolean;
	calendar: boolean;
	contacts: boolean;
}

export interface Account {
	id: number;
	server_id: number;
	email: string;
	login: string | null;
	has_password: boolean;
	status: AccountStatus;
	last_test: number | null;
	last_test_result: TestResult | null;
	pst_id: number | null;
	/** Where the items come from. kind 'pst' (or a missing source) = the linked PST. */
	source?: Endpoint;
	/** The account's own target. kind null = the account's server. */
	target?: Endpoint;
	options: AccountOptions;
	job: Job | null;
}

export type JobStatus = 'queued' | 'running' | 'paused' | 'stopping' | 'stopped' | 'done' | 'done_with_errors' | 'failed';
export type JobPhase = 'precheck' | 'mail' | 'calendar' | 'contacts' | 'verify';
export interface StreamProgress {
	done: number;
	total: number;
	errors: number;
}

export interface Job {
	id: number;
	account_id: number;
	status: JobStatus;
	phase: JobPhase | null;
	progress: { mail: StreamProgress; calendar: StreamProgress; contacts: StreamProgress };
	current_folder: string | null;
	rate: number | null;
	rate_bytes: number | null;
	eta: number | null;
	started_at: number | null;
	finished_at: number | null;
	error: string | null;
	mode?: string | null;
	created_at?: number | null;
	tuning: { mode: string; builders: number; connections: number; fallback?: boolean } | null;
}

export interface LogLine {
	n: number;
	ts: number | string | null;
	level: string;
	msg: string;
}

export interface JobError {
	id: number;
	kind: string;
	folder: string | null;
	item_key: string | null;
	subject: string | null;
	error: string | null;
	updated_at?: number | string | null;
}

export interface ScanFolder {
	path: string[] | string;
	kind: string;
	count: number;
}

export type PstStatus = 'uploading' | 'validating' | 'valid' | 'invalid' | 'fetching' | 'cancelling' | 'fetch_failed';
export type PstSource = 'upload' | 'inbox' | 'fetch';

/** Outcome of the automatic assignment; unknown results and extra fields are kept as served. */
export interface PstChain {
	result: 'assigned' | 'ambiguous' | 'no_match' | 'account_has_pst' | 'already_assigned' | 'error' | (string & {});
	account_id?: number;
	job_id?: number;
	error?: string;
	[key: string]: unknown;
}

export interface Pst {
	id: number;
	filename: string;
	size: number;
	status: PstStatus;
	scan: { folders?: ScanFolder[]; totals?: Record<string, number>; errors?: unknown[] } | null;
	error: string | null;
	pst_sha?: string | null;
	created_at?: number | null;
	updated_at?: number | null;
	source?: PstSource;
	/** Label without secrets: the URL host, the drop folder, ... */
	origin?: string | null;
	/** Progress while fetching; `size` is 0 until the remote total is known. */
	received_bytes?: number | null;
	chain?: PstChain | null;
}

export type FetchKind = 'http' | 'sftp' | 's3';

export interface UploadInfo {
	id: number;
	filename?: string;
	size?: number;
	chunk_size: number;
	received: number[];
	status?: string;
	pst_id?: number;
}

export interface CsvResult {
	created: number;
	updated: number;
	errors: { line: number; msg: string }[];
}

export interface Health {
	ok: boolean;
	worker?: { alive: boolean; last_seen: number | null };
	version?: string;
}

/** GET /api/connectors: the catalogue. Forms are drawn from `fields`, so a kind the UI has never seen still works. */
export type ConnectorFieldType = 'text' | 'password' | 'number' | 'boolean' | 'select' | 'url' | (string & {});

export interface ConnectorField {
	name: string;
	label: string;
	type: ConnectorFieldType;
	required: boolean;
	secret: boolean;
	default: unknown;
	options?: string[];
	help?: string;
}

export interface ConnectorRole {
	items: string[];
	capabilities: string[];
	fields: ConnectorField[];
}

export interface Connector {
	kind: string;
	label: string;
	description: string;
	source: ConnectorRole | null;
	target: ConnectorRole | null;
}

/** Source or own target of an account as the API shows it. Secrets never come back. */
export interface Endpoint {
	kind: string | null;
	config: Record<string, unknown>;
	has_secret: boolean;
}

export interface EndpointInput {
	kind: string | null;
	config?: Record<string, unknown>;
	/** Omitted keeps the stored secrets, {} clears them, a value replaces that field (empty removes it). */
	secrets?: Record<string, string>;
}

export type ConnectorErrorKind = 'auth' | 'config' | 'unreachable' | 'transient' | 'rejected' | 'quota' | 'folder_missing';

export interface ConnectorProbe {
	ok: boolean;
	msg: string;
	error_kind?: ConnectorErrorKind | (string & {});
	folders?: { path: string[] | string; kind: string; special?: string | null; count: number }[];
	quota?: unknown;
}

/** GET/PUT /api/intake/settings. Secrets are write-only: `has_*` says they are set. */
export type NotificationEvent =
	| 'upload_finished'
	| 'analysis_valid'
	| 'analysis_invalid'
	| 'job_done'
	| 'job_done_with_errors'
	| 'job_failed'
	| 'job_stopped'
	| 'phase_mail'
	| 'phase_calendar'
	| 'phase_contacts'
	| 'batch_finished'
	| 'disk_low'
	| 'repeated_crash';

export interface NotifyChannels {
	ntfy: { enabled: boolean; url: string; topic: string; priority: number; has_token: boolean };
	webhook: { enabled: boolean; url: string; has_token: boolean };
	email: { enabled: boolean; host: string; port: number; security: 'ssl' | 'starttls' | 'none'; username: string; from: string; to: string; has_password: boolean };
}

export interface IntakeSettings {
	notifications: {
		enabled: boolean;
		mask_addresses: boolean;
		group_window: number;
		disk_low_mb: number;
		events: Record<NotificationEvent, boolean>;
	} & NotifyChannels;
	inbox: { enabled: boolean; settle_seconds: number; path: string };
	auto_chain: { enabled: boolean; start_import: boolean; default_domains: Record<string, string> };
}

/** PUT body: partial at every level; secrets (`token`, `password`) only when changed, "" clears. */
export type IntakeSettingsPatch = {
	notifications?: Partial<Omit<IntakeSettings['notifications'], 'ntfy' | 'webhook' | 'email' | 'events'>> & {
		events?: Partial<Record<NotificationEvent, boolean>>;
		ntfy?: Partial<Omit<NotifyChannels['ntfy'], 'has_token'>> & { token?: string };
		webhook?: Partial<Omit<NotifyChannels['webhook'], 'has_token'>> & { token?: string };
		email?: Partial<Omit<NotifyChannels['email'], 'has_password'>> & { password?: string };
	};
	inbox?: Partial<Omit<IntakeSettings['inbox'], 'path'>>;
	auto_chain?: Partial<IntakeSettings['auto_chain']>;
};

export type NotifyChannel = 'ntfy' | 'webhook' | 'email';

export interface NotifyTestResult {
	ok: boolean;
	results: Record<string, { ok: boolean; error: string | null }>;
}

export interface InboxListing {
	path: string;
	enabled: boolean;
	settle_seconds: number;
	files: { name: string; size: number; modified_ago: number }[];
}
