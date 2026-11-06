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

export interface Pst {
	id: number;
	filename: string;
	size: number;
	status: 'uploading' | 'validating' | 'valid' | 'invalid';
	scan: { folders?: ScanFolder[]; totals?: Record<string, number>; errors?: unknown[] } | null;
	error: string | null;
	pst_sha?: string | null;
	created_at?: number | null;
	updated_at?: number | null;
}

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

/**
 * GET /api/connectors, added by another branch. Only a name is assumed: any other field
 * the server sends as text is shown as served, and a missing route hides the list.
 */
export interface Connector {
	id?: string;
	kind?: string;
	name?: string;
	label?: string;
	description?: string;
}
