/** What the "Fetch from a server" dialog asks for, per kind, and the request body built from it. */
import type { FetchKind } from '../api/types.ts';

export const FETCH_KINDS: FetchKind[] = ['http', 'sftp', 's3'];

export type FetchFieldName =
	| 'url' | 'token' | 'username' | 'password' | 'filename'
	| 'host' | 'port' | 'path' | 'private_key' | 'passphrase' | 'host_key_sha256'
	| 'endpoint' | 'bucket' | 'key' | 'region' | 'access_key' | 'secret_key';

export interface FetchField {
	name: FetchFieldName;
	required?: boolean;
	/** Tucked behind "More options". */
	advanced?: boolean;
	secret?: boolean;
	number?: boolean;
	area?: boolean;
	placeholder?: string;
}

export const FETCH_FIELDS: Record<FetchKind, FetchField[]> = {
	http: [
		{ name: 'url', required: true, placeholder: 'https://files.example.com/jane.pst' },
		{ name: 'token', secret: true, advanced: true },
		{ name: 'username', advanced: true },
		{ name: 'password', secret: true, advanced: true },
		{ name: 'filename', advanced: true }
	],
	sftp: [
		{ name: 'host', required: true },
		{ name: 'path', required: true, placeholder: '/backups/jane.pst' },
		{ name: 'username', required: true },
		{ name: 'password', secret: true },
		{ name: 'private_key', secret: true, area: true, advanced: true },
		{ name: 'passphrase', secret: true, advanced: true },
		{ name: 'port', number: true, advanced: true, placeholder: '22' },
		{ name: 'host_key_sha256', advanced: true },
		{ name: 'filename', advanced: true }
	],
	s3: [
		{ name: 'endpoint', required: true, placeholder: 'https://s3.eu-west-1.amazonaws.com' },
		{ name: 'bucket', required: true },
		{ name: 'key', required: true },
		{ name: 'access_key', required: true },
		{ name: 'secret_key', required: true, secret: true },
		{ name: 'region', advanced: true, placeholder: 'us-east-1' },
		{ name: 'filename', advanced: true }
	]
};

export type FetchValues = Partial<Record<FetchFieldName, string>>;

export function missingFetchFields(kind: FetchKind, values: FetchValues): FetchFieldName[] {
	const out = FETCH_FIELDS[kind].filter((f) => f.required && !values[f.name]?.trim()).map((f) => f.name);
	// SFTP needs a password or a key.
	if (kind === 'sftp' && !values.password && !values.private_key?.trim()) out.push('password');
	return out;
}

/** Empty fields are left out and the port becomes a number. Secrets are sent as typed, the rest trimmed. */
export function buildFetchBody(kind: FetchKind, values: FetchValues): Record<string, unknown> {
	const body: Record<string, unknown> = { kind };
	for (const f of FETCH_FIELDS[kind]) {
		const raw = values[f.name];
		if (raw == null || raw === '') continue;
		if (f.secret) body[f.name] = raw;
		else if (f.number) {
			const n = Number(raw);
			if (Number.isFinite(n)) body[f.name] = Math.trunc(n);
		} else if (raw.trim()) body[f.name] = raw.trim();
	}
	return body;
}
