/**
 * Pure helpers behind the generic connector form. Nothing here knows a kind by name:
 * the catalogue (GET /api/connectors) says which fields a role has.
 */
import type { Connector, ConnectorField, ConnectorRole, Endpoint, EndpointInput } from '../api/types.ts';

export type Role = 'source' | 'target';
export type Values = Record<string, unknown>;
/** What was typed into a secret field. `clear` is an explicit "remove the stored value". */
export interface SecretDraft {
	value: string;
	clear: boolean;
}
export type SecretDrafts = Record<string, SecretDraft>;

/** The PST is the historical source and keeps its own control in the table. */
export const PST_KIND = 'pst';

export function kindsFor(catalogue: readonly Connector[], role: Role): Connector[] {
	return catalogue.filter((c) => c[role] != null && !(role === 'source' && c.kind === PST_KIND));
}

export function findKind(catalogue: readonly Connector[], kind: string | null | undefined): Connector | undefined {
	return kind ? catalogue.find((c) => c.kind === kind) : undefined;
}

/** Name shown for a kind: the catalogue label, or the kind itself when the catalogue does not know it. */
export function kindLabel(catalogue: readonly Connector[], kind: string): string {
	return findKind(catalogue, kind)?.label || kind;
}

export function fieldsOf(role: ConnectorRole | null | undefined): ConnectorField[] {
	return role?.fields ?? [];
}

export const isSecret = (f: ConnectorField) => f.secret || f.type === 'password';

/** Values to start the form with: what is stored, else the catalogue default. Secrets are never prefilled. */
export function initialValues(role: ConnectorRole | null | undefined, stored: Values | undefined): Values {
	const out: Values = {};
	for (const f of fieldsOf(role)) {
		if (f.secret) continue;
		const v = stored?.[f.name] ?? f.default;
		out[f.name] = v ?? (f.type === 'boolean' ? false : '');
	}
	return out;
}

export function initialSecrets(role: ConnectorRole | null | undefined): SecretDrafts {
	const out: SecretDrafts = {};
	for (const f of fieldsOf(role)) if (f.secret) out[f.name] = { value: '', clear: false };
	return out;
}

/** Config for the API: empty text is left out (the server applies the default), numbers become numbers. */
export function buildConfig(role: ConnectorRole | null | undefined, values: Values): Values {
	const out: Values = {};
	for (const f of fieldsOf(role)) {
		if (f.secret) continue;
		const v = values[f.name];
		if (v === '' || v == null) continue;
		if (f.type === 'number') {
			const n = Number(v);
			if (Number.isFinite(n)) out[f.name] = Math.trunc(n);
		} else if (f.type === 'boolean') {
			out[f.name] = v === true;
		} else {
			out[f.name] = typeof v === 'string' ? v.trim() : v;
		}
	}
	return out;
}

/**
 * Secrets for the API, or undefined when nothing was touched (the server keeps what it has).
 * A typed value replaces that field, an explicit clear sends "" for it.
 */
export function buildSecrets(drafts: SecretDrafts): Record<string, string> | undefined {
	const out: Record<string, string> = {};
	for (const [name, d] of Object.entries(drafts)) {
		if (d.value) out[name] = d.value;
		else if (d.clear) out[name] = '';
	}
	return Object.keys(out).length ? out : undefined;
}

/** Required fields still empty. A stored secret counts as filled unless it is being cleared or replaced. */
export function missingRequired(role: ConnectorRole | null | undefined, values: Values, drafts: SecretDrafts, hasSecret: boolean): string[] {
	const out: string[] = [];
	for (const f of fieldsOf(role)) {
		if (!f.required) continue;
		if (f.secret) {
			const d = drafts[f.name];
			const kept = hasSecret && !d?.clear;
			if (!d?.value && !kept) out.push(f.name);
		} else if (f.type !== 'boolean' && (values[f.name] === '' || values[f.name] == null)) {
			// A default is applied by the server.
			if (f.default == null || f.default === '') out.push(f.name);
		}
	}
	return out;
}

export function endpointBody(kind: string, role: ConnectorRole | null | undefined, values: Values, drafts: SecretDrafts): EndpointInput {
	const body: EndpointInput = { kind, config: buildConfig(role, values) };
	const secrets = buildSecrets(drafts);
	if (secrets) body.secrets = secrets;
	return body;
}

/** One short line for a stored endpoint: its label and the first plain value of its config. */
export function endpointSummary(catalogue: readonly Connector[], endpoint: Endpoint | null | undefined, role: Role): { kind: string; label: string; detail: string } | null {
	const kind = endpoint?.kind;
	if (!kind || (role === 'source' && kind === PST_KIND)) return null;
	const fields = fieldsOf(findKind(catalogue, kind)?.[role]);
	const config = endpoint.config ?? {};
	const names = fields.length ? fields.filter((f) => !f.secret && f.type !== 'boolean').map((f) => f.name) : Object.keys(config);
	let detail = '';
	for (const name of names) {
		const v = config[name];
		if (typeof v === 'string' && v) {
			detail = v;
			break;
		}
	}
	return { kind, label: kindLabel(catalogue, kind), detail };
}

/** True when the account reads from something other than its PST. */
export const hasCustomSource = (a: { source?: Endpoint }) => !!a.source?.kind && a.source.kind !== PST_KIND;
export const hasOwnTarget = (a: { target?: Endpoint }) => !!a.target?.kind;
