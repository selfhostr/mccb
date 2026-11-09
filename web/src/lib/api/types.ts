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
	/** Last connection test, stored by the server; cleared when a connection setting changes. */
	last_test?: number | null;
	last_test_result?: TestResult | null;
}

export type ServerInput = Omit<Server, 'id' | 'last_test' | 'last_test_result'>;

export interface ProbeResult {
	ok: boolean;
	msg: string;
	quota?: unknown;
	/** Server test with DAV switched off: true when the CalDAV/CardDAV entry point answers anyway. */
	found?: boolean;
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
	/** Higher runs first among queued jobs. */
	priority?: number;
	batch_id?: number | null;
	/** 'window': waiting for the allowed hours of the server, resumes by itself. null: paused by the administrator. */
	pause_reason?: 'window' | null;
	account_id: number;
	status: JobStatus;
	phase: JobPhase | null;
	progress: { mail: StreamProgress; calendar: StreamProgress; contacts: StreamProgress; tasks?: StreamProgress };
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
	scan: {
		/** 'files' for an upload that is not a PST (mbox, .eml, .msg, .ics, .vcf, .zip). */
		format?: string;
		/** The formats found in a 'files' upload. */
		contents?: string[];
		folders?: ScanFolder[];
		totals?: Record<string, number>;
		errors?: unknown[];
		n_skipped?: number;
	} | null;
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
	warnings?: { line: number; msg: string }[];
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
	/** 'connection' fields live on a source connection (tenant), 'mailbox' ones on the account. */
	scope?: 'mailbox' | 'connection';
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
	/** True when the connector can register a connection and list its mailboxes (m365_graph). */
	directory?: boolean;
	/** True when "Connect with Microsoft" is available (a bootstrap app is configured). */
	provision?: boolean;
}

/** Source or own target of an account as the API shows it. Secrets never come back. */
export interface Endpoint {
	kind: string | null;
	config: Record<string, unknown>;
	has_secret: boolean;
	/** Set when the source is read through a source connection (tenant). */
	connection?: { id: number; name: string } | null;
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

/** POST /api/runs body. `name` makes a batch; `mode: 'delta'` is the final resync. */
export interface RunInput {
	account_ids: number[];
	name?: string;
	mode?: 'full' | 'delta';
}

export type PlanStatus = 'queued' | 'running' | 'done' | 'failed';
export type PlanAccountStatus = 'ok' | 'warning' | 'error';
export type PlanKind = 'mail' | 'calendar' | 'contacts';

export interface PlanKindTotals {
	folders: number;
	items: number;
	bytes: number | null;
	done: number;
	to_migrate: number;
	to_migrate_bytes: number | null;
	bytes_estimated?: boolean;
	filtered: Record<string, number>;
	oversized: number;
	oversized_bytes: number;
	folders_to_create: number;
	excluded_folders: number;
	excluded_items: number;
}

export interface PlanFolder {
	path: string[] | string;
	kind?: string;
	items: number;
	bytes: number | null;
	to_migrate: number;
	/** The folder it will land in. */
	target: string | null;
	/** null when the target cannot say (DAV). */
	create: boolean | null;
	excluded: boolean;
	filtered?: Record<string, number>;
	oversized?: number;
	error?: string;
}

export interface PlanAccount {
	account_id: number;
	email?: string | null;
	status: PlanAccountStatus;
	error?: string;
	source?: { kind: string | null; ok: boolean; msg: string; error_kind?: string } | null;
	targets?: Record<string, { ok: boolean; msg?: string; quota?: unknown; append_limit?: number | null }>;
	kinds?: Partial<Record<PlanKind, PlanKindTotals>>;
	/** Absent in events. */
	folders?: PlanFolder[];
	quota?: { used: number | null; limit: number | null; free: number; need: number; enough: boolean } | null;
	target_limit?: number | null;
	warnings?: string[];
	notes?: string[];
}

export interface PlanResult {
	accounts: PlanAccount[];
	totals: { accounts: number; ok: number; warning: number; error: number; to_migrate: number; to_migrate_bytes: number; oversized: number; filtered: number };
	done: number;
	total: number;
}

/** POST /api/runs/plan (202) then GET /api/plans/{id} until `status` is done or failed. */
export interface Plan {
	id: number;
	status: PlanStatus;
	account_ids: number[];
	error: string | null;
	result: PlanResult | null;
}

export type BatchState = 'queued' | 'running' | 'paused' | 'finished' | 'empty';
export type BatchAction = 'pause' | 'resume' | 'stop' | 'retry-failed';

export interface Batch {
	id: number;
	name: string;
	created_at: number;
	state: BatchState;
	jobs: number;
	by_status: Partial<Record<JobStatus, number>>;
	items: Record<PlanKind, StreamProgress>;
	failed: number;
	with_errors: number;
	finished_at: number | null;
	/** Only from GET /api/batches/{id} and the actions. */
	changed?: number;
	skipped?: number;
}

export interface FolderRule {
	source: string;
	target: string;
}

export interface PolicyWindow {
	days?: number[];
	start: string;
	end: string;
	tz: string;
}

/** Server or account policy. `window` and `rate_mb_s` belong to servers. */
export interface Policy {
	folder_map?: FolderRule[];
	exclude_folders?: string[];
	date_from?: string;
	date_to?: string;
	max_size_mb?: number;
	window?: PolicyWindow;
	rate_mb_s?: number;
}

/** GET /api/servers/{id}/policy and /api/accounts/{id}/policy (the account also gets `effective`). */
export interface PolicyDoc {
	policy: Policy | null;
	effective?: Policy;
}

export interface CompareFolder {
	kind: string;
	target: string | null;
	folders: string[];
	expected: number;
	on_target: number | null;
	missing: number;
	extra?: number | null;
	sent: number;
	errors: number;
	pending: number;
	filtered: number;
	skipped: number;
	error?: string;
}

export interface Compare {
	id?: number;
	status: 'queued' | 'running' | 'done' | 'failed' | 'none';
	error?: string | null;
	result: {
		ok: boolean;
		totals: Record<string, number>;
		folders: CompareFolder[];
	} | null;
}

/** GET /api/jobs/{id}/filtered and /oversized. */
export interface LeftOutItem {
	id: number;
	kind: string;
	folder: string | null;
	item_key: string | null;
	subject: string | null;
	size: number | null;
	error: string | null;
	reason?: string;
}

export interface ApiToken {
	id: number;
	name: string;
	prefix: string;
	created_at: number;
	last_used_at: number | null;
	revoked_at: number | null;
}

/** POST /api/tokens: `token` is in this answer only. */
export interface CreatedToken extends ApiToken {
	token: string;
}

// ---------------------------------------------------------------- repair tab (docs/repair.md)
export type RepairIssue = 'replacement_chars' | 'mojibake' | 'attachment_names' | 'detached_images' | 'unreadable';
export type RepairMode = 'scan' | 'repair' | 'restore';
export type RepairRunStatus = 'queued' | 'running' | 'stopping' | 'stopped' | 'done' | 'done_with_errors' | 'failed';

export interface RepairProgress {
	total: number;
	seen: number;
	affected: number;
	repaired: number;
	restored: number;
	skipped: number;
	errors: number;
	folder: string | null;
	folders_done: number;
	folders_total: number;
}

export interface RepairFolder {
	seen: number;
	affected: number;
	repaired: number;
	issues: Partial<Record<RepairIssue, number>>;
}

export interface RepairExample {
	folder: string;
	uid: string;
	subject: string;
	date: string;
	issues: RepairIssue[];
	fixed: RepairIssue[] | null;
}

export interface RepairReport {
	issues: Partial<Record<RepairIssue, number>>;
	folders: Record<string, RepairFolder>;
	examples: RepairExample[];
	errors: { folder: string; uid: string | null; msg: string }[];
	notes: string[];
}

export interface RepairRun {
	id: number;
	box_id: number;
	mode: RepairMode;
	status: RepairRunStatus;
	folder: string | null;
	progress: RepairProgress;
	error: string | null;
	created_at: number;
	started_at: number | null;
	finished_at: number | null;
	issues: Partial<Record<RepairIssue, number>>;
	report?: RepairReport;
}

export interface RepairBox {
	id: number;
	server_id: number;
	email: string;
	login: string;
	has_password: boolean;
	status: AccountStatus;
	last_test: number | null;
	last_test_result: TestResult | null;
	created_at: number;
	last_scan: RepairRun | null;
	active_run: RepairRun | null;
}

export interface RepairSummary {
	subject: string;
	text: string;
	parts: { name: string; type: string; inline: boolean; size: number | null }[];
}

export interface RepairPreview {
	issues: RepairIssue[];
	already: boolean;
	before: RepairSummary;
	after: RepairSummary | null;
	fixes: RepairIssue[];
	left: RepairIssue[];
	error: string | null;
}

/** Source connections (Microsoft 365 tenants). Secrets never come back. */
export interface CheckItem {
	key: string;
	ok: boolean;
	level: 'required' | 'optional';
	msg: string;
	missing?: string[];
}

export interface CheckResult {
	ok: boolean;
	checks: CheckItem[];
	tenant_name?: string;
}

export interface SourceConnection {
	id: number;
	name: string;
	kind: string;
	config: Record<string, unknown>;
	has_secret: boolean;
	notes: string | null;
	last_test: number | null;
	last_test_result: CheckResult | null;
	accounts: number;
	/** Set when the app was created in the tenant by "Connect with Microsoft". */
	provisioned?: { app_object_id: string; sp_object_id: string; created_at: number } | null;
	created_at: number;
}

export interface ProvisionFlow {
	flow_id: string;
	user_code: string;
	verification_uri: string;
	expires_in: number;
	interval: number;
	message: string;
}

export type ProvisionStatus = 'waiting' | 'working' | 'done' | 'error' | 'expired';
export type ProvisionStep = 'signin' | 'app' | 'consent' | 'secret' | 'propagation' | 'test' | 'remove';

/** Why a flow failed (or ended with a warning), stable: the UI translates it, `error` stays the English details. */
export type ProvisionErrorCode =
	| 'not_admin'
	| 'consent_refused'
	| 'expired'
	| 'step_refused'
	| 'propagation_timeout'
	| 'login_refused'
	| 'unreachable'
	| 'other_tenant'
	| 'cancelled'
	| 'unexpected';

export interface ProvisionState {
	status: ProvisionStatus;
	step: ProvisionStep | null;
	error: string | null;
	error_code: ProvisionErrorCode | null;
	connection: SourceConnection | null;
}

export interface SourceConnectionInput {
	name: string;
	kind: string;
	config: Record<string, unknown>;
	/** On update: omitted keeps stored secrets, {} clears them, a value replaces that field. */
	secrets?: Record<string, string>;
	notes?: string | null;
}

export type TenantMailboxKind = 'user' | 'shared' | 'room' | 'equipment' | 'unknown';

export interface TenantMailbox {
	id: string;
	display_name: string;
	mail: string;
	upn: string;
	aliases: string[];
	kind: TenantMailboxKind;
	enabled: boolean;
	licensed: boolean;
}

export interface TenantMailboxes {
	fetched_at: number;
	purpose_checked: boolean;
	mailboxes: TenantMailbox[];
	hidden: { guests: number; no_mailbox: number };
}

export type MatchState = 'ready' | 'linked' | 'other_source' | 'no_target' | 'no_source' | 'conflict';
export type CoverageState = 'on' | 'off' | 'no_dav' | 'failed' | 'unknown' | 'no_permission';

export interface Coverage {
	mail: CoverageState;
	calendar: CoverageState;
	contacts: CoverageState;
	tasks: CoverageState;
}

/** The server side of a matching row: the mccb account, as the matching screen needs it. */
export interface MatchAccount {
	id: number;
	email: string;
	status: AccountStatus;
	last_test: number | null;
	last_test_result: TestResult | null;
	/** Source kind of the account today (null or 'pst' without a valid file: free to link). */
	source_kind: string | null;
	/** Set when the account is already read through a source connection. */
	source_connection_id: number | null;
	/** No source of its own worth keeping, or already read through this tenant: it can be chosen as a destination. */
	free?: boolean;
}

/**
 * One row of the matching screen, one pair (or one lone side).
 * - mailbox is null only for 'no_source', account is null only for 'no_target';
 * - matched_by and matched_address say how the pair was made ('alias' shows "via alias x", 'manual': the destination
 *   was chosen by hand, the account is linked to this mailbox whatever its address);
 * - conflict_with: for 'conflict', the address of the tenant mailbox that won the account;
 * - last_job: the account's latest job (linked rows show it), null when none.
 */
export interface MatchRow {
	state: MatchState;
	mailbox: TenantMailbox | null;
	account: MatchAccount | null;
	matched_by: 'mail' | 'upn' | 'alias' | 'manual' | null;
	matched_address: string | null;
	conflict_with?: string | null;
	coverage: Coverage;
	last_job: { id: number; status: JobStatus } | null;
}

export interface MatchResponse {
	connection: { id: number; name: string };
	server: { id: number; name: string; dav_enabled: boolean; caldav_ok: boolean | null; carddav_ok: boolean | null };
	rows: MatchRow[];
	counts: Partial<Record<MatchState, number>>;
	/** From the mailbox listing the match was computed on. */
	fetched_at: number;
	purpose_checked: boolean;
	hidden: { guests: number; no_mailbox: number };
}

export interface LinkInput {
	server_id: number;
	/** manual: a destination chosen by hand (any other box of the server linked to this mailbox lets it go). */
	links: { account_id: number; mailbox_id: string; manual?: boolean }[];
	include_tasks?: boolean;
	start?: boolean;
	mode?: 'full' | 'delta';
	batch_name?: string;
}

export interface LinkResponse {
	linked: number[];
	errors: { account_id: number; msg: string }[];
	jobs: Job[];
	batch_id: number | null;
}
