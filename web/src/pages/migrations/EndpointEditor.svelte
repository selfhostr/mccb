<script lang="ts">
	/**
	 * One side of an account's route: the source or its own target. A kind select, the generic form of that kind
	 * and a test button. The parent asks `body()` when saving.
	 */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { Account, ConnectorProbe, EndpointInput } from '../../lib/api/types.ts';
	import {
		endpointBody,
		findKind,
		initialSecrets,
		initialValues,
		kindLabel,
		kindsFor,
		missingRequired,
		isFileKind,
		PST_KIND,
		buildConfig,
		buildSecrets,
		type Role,
		type SecretDrafts,
		type Values
	} from '../../lib/logic/connector.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import ConnectorForm from './ConnectorForm.svelte';

	interface Props {
		role: Role;
		account: Account;
		/** Called on every edit, so the parent can show what saving will do. */
		onedit?: () => void;
		/** A kind picked elsewhere (the new migration's tiles): the select is not shown. */
		fixedKind?: string;
	}

	let { role, account, onedit, fixedKind }: Props = $props();

	// What is stored when the dialog opens. The editor is mounted per role and per opening, so props do not change under it.
	const stored = untrack(() => account[role]);
	const storedKind = untrack(() => (stored?.kind && !(role === 'source' && isFileKind(stored.kind)) ? stored.kind : ''));

	let kind = $state(storedKind);
	let values = $state<Values>({});
	let drafts = $state<SecretDrafts>({});
	let dirty = $state(false);
	let probe = $state<ConnectorProbe | null>(null);
	let probeError = $state<string | null>(null);
	let testing = $state(false);
	let problem = $state<string | null>(null);

	const options = $derived(kindsFor(connectors.list, role));
	const def = $derived(findKind(connectors.list, kind)?.[role] ?? null);
	const sameAsStored = $derived(kind === storedKind);
	const hasSecret = $derived(sameAsStored && !!stored?.has_secret);

	function applyKind(next: string) {
		kind = next;
		const d = findKind(connectors.list, next)?.[role] ?? null;
		values = initialValues(d, next === storedKind ? stored?.config : undefined);
		drafts = initialSecrets(d);
		probe = null;
		probeError = null;
		problem = null;
	}
	applyKind(untrack(() => fixedKind) ?? storedKind);

	function edited() {
		dirty = true;
		problem = null;
		onedit?.();
	}

	function missingNames(): string[] {
		const names = missingRequired(def, values, drafts, hasSecret);
		return names.map((n) => def?.fields.find((f) => f.name === n)?.label ?? n);
	}

	/** What to send: nothing when untouched, or the body; a message when a required field is empty. */
	export function body(): { skip: true } | { input: EndpointInput } | { problem: string } {
		if (!kind) return storedKind ? { input: { kind: role === 'source' ? PST_KIND : null } } : { skip: true };
		if (!dirty && sameAsStored) return { skip: true };
		const missing = missingNames();
		if (missing.length) {
			problem = m.migrations_route_missing({ fields: missing.join(', ') });
			return { problem };
		}
		return { input: endpointBody(kind, def, values, drafts) };
	}

	export const changed = () => (kind ? dirty || !sameAsStored : !!storedKind);

	async function test() {
		const missing = missingNames();
		if (missing.length) {
			problem = m.migrations_route_missing({ fields: missing.join(', ') });
			return;
		}
		testing = true;
		probe = null;
		probeError = null;
		try {
			const secrets = buildSecrets(drafts);
			probe = await post<ConnectorProbe>(`/connectors/${encodeURIComponent(kind)}/test`, {
				role,
				config: buildConfig(def, values),
				...(secrets ? { secrets } : {}),
				...(account.id > 0 ? { account_id: account.id } : {})
			});
		} catch (error) {
			probeError = errorText(error);
		} finally {
			testing = false;
		}
	}

	const KIND_WORDS = $derived<Record<string, string>>({
		auth: m.migrations_route_err_auth(),
		config: m.migrations_route_err_config(),
		unreachable: m.migrations_route_err_unreachable(),
		transient: m.migrations_route_err_transient(),
		rejected: m.migrations_route_err_rejected(),
		quota: m.migrations_route_err_quota(),
		folder_missing: m.migrations_route_err_folder_missing()
	});
</script>

<!-- Edits bubble up from the inputs: one place marks the side as touched. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="flex flex-col gap-3" data-testid={`route-${role}`} oninput={edited} onchange={edited} role="group" aria-label={role === 'source' ? m.migrations_route_source() : m.migrations_route_target()}>
	{#if !fixedKind}
	<label class="flex min-w-0 flex-col gap-1">
		<span class="font-medium text-ink-2">{role === 'source' ? m.migrations_route_source() : m.migrations_route_target()}</span>
		<select
			class="input"
			value={kind}
			onchange={(e) => applyKind(e.currentTarget.value)}
			data-testid={`route-${role}-kind`}
		>
			<option value="">{role === 'source' ? m.migrations_route_source_pst() : m.migrations_route_target_server()}</option>
			{#each options as option (option.kind)}
				<option value={option.kind}>{option.label || option.kind}</option>
			{/each}
			{#if kind && !options.some((o) => o.kind === kind)}<option value={kind}>{kindLabel(connectors.list, kind)}</option>{/if}
		</select>
		<small class="text-xs text-ink-3">{kind ? (findKind(connectors.list, kind)?.description ?? '') : role === 'source' ? m.migrations_route_source_pst_hint() : m.migrations_route_target_server_hint()}</small>
	</label>
	{/if}

	{#if kind && def}
		<ConnectorForm role={def} bind:values bind:drafts {hasSecret} />
		<div class="flex flex-wrap items-center gap-2">
			<Button icon={FlaskConical} loading={testing} onclick={test} data-testid={`route-${role}-test`}>{m.migrations_route_test()}</Button>
			{#if probe}
				<span class="inline-flex min-w-0 items-center gap-2" data-testid={`route-${role}-result`}>
					{#if probe.ok}
						<Plate tone="signal" icon={CircleCheck} label={m.migrations_route_test_ok()} />
					{:else}
						<Plate tone="warning" icon={TriangleAlert} label={(probe.error_kind && KIND_WORDS[probe.error_kind]) || probe.error_kind || m.migrations_route_test_failed()} />
					{/if}
				</span>
			{/if}
		</div>
		{#if probe}<p class="m-0 font-mono text-xs break-words text-ink-2">{prefs.text(probe.msg)}</p>{/if}
		{#if probeError}<p class="m-0 text-xs text-warning-ink" role="alert">{probeError}</p>{/if}
	{:else if kind}
		<!-- A kind the catalogue no longer has for this role: shown, not editable. -->
		<p class="m-0 text-xs text-warning-ink">{m.migrations_route_unknown({ kind })}</p>
	{/if}
	{#if problem}<p class="m-0 text-xs text-warning-ink" role="alert" data-testid={`route-${role}-problem`}>{problem}</p>{/if}
</div>
