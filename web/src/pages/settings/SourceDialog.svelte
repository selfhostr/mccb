<script lang="ts">
	/** Adds or edits a Microsoft 365 tenant: connection-scoped fields of the catalogue, a Test before saving. */
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Cloud from '@lucide/svelte/icons/cloud';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import { docsUrl } from '../../lib/base.ts';
	import type { CheckResult, ConnectorRole, SourceConnection } from '../../lib/api/types.ts';
	import { buildConfig, buildSecrets, initialSecrets, initialValues, missingRequired, type SecretDrafts, type Values } from '../../lib/logic/connector.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ConnectorForm from '../migrations/ConnectorForm.svelte';
	import CheckList from './CheckList.svelte';
	import ProvisionPanel from './ProvisionPanel.svelte';

	const KIND = 'm365_graph';
	interface Props {
		connection?: SourceConnection | null;
		onclose: () => void;
		onsaved?: (saved: SourceConnection) => void;
	}
	let { connection = null, onclose, onsaved }: Props = $props();

	const full = $derived(connectors.list.find((c) => c.kind === KIND)?.source ?? null);
	const canProvision = $derived(!connection && connectors.list.find((c) => c.kind === KIND)?.provision === true);
	// The labels and helps of the connection fields are served in English: the dialog says them in the language of the UI.
	const TEXTS: Record<string, () => { label: string; help?: string }> = {
		tenant_id: () => ({ label: m.provision_field_tenant_id(), help: m.provision_field_tenant_id_help() }),
		client_id: () => ({ label: m.provision_field_client_id(), help: m.provision_field_client_id_help() }),
		client_secret: () => ({ label: m.provision_field_client_secret(), help: m.provision_field_client_secret_help() }),
		cloud: () => ({ label: m.provision_field_cloud(), help: m.provision_field_cloud_help() })
	};
	const role = $derived<ConnectorRole | null>(full ? { ...full, fields: full.fields.filter((f) => f.scope === 'connection').map((f) => ({ ...f, ...(TEXTS[f.name]?.() ?? {}) })) } : null);
	const clouds = $derived(full?.fields.find((f) => f.name === 'cloud')?.options ?? ['global']);

	// "Connect with Microsoft": the flow replaces the form while it runs.
	let flowing = $state<'provision' | null>(null);
	let manualOpen = $state(false);
	let cloud = $state('global');
	$effect(() => {
		if (!canProvision) manualOpen = true;
	});

	let name = $state(untrack(() => connection?.name ?? ''));
	let notes = $state(untrack(() => connection?.notes ?? ''));
	let values = $state<Values>({});
	let drafts = $state<SecretDrafts>({});
	let ready = $state(false);
	// The catalogue may arrive after the dialog opens: the form is built once it is there.
	$effect(() => {
		if (!role || ready) return;
		untrack(() => {
			values = initialValues(role, connection?.config);
			drafts = initialSecrets(role);
			ready = true;
		});
	});

	let busy = $state(false);
	let testing = $state(false);
	let error = $state<string | null>(null);
	let problem = $state<string | null>(null);
	let result = $state<CheckResult | null>(untrack(() => connection?.last_test_result ?? null));
	let testError = $state<string | null>(null);

	const hasSecret = $derived(!!connection?.has_secret);
	const locked = $derived((connection?.accounts ?? 0) > 0);

	function missing(): string | null {
		const names = missingRequired(role, values, drafts, hasSecret).map((n) => role?.fields.find((f) => f.name === n)?.label ?? n);
		return names.length ? m.sources_test_need({ fields: names.join(', ') }) : null;
	}

	async function test() {
		problem = missing();
		if (problem) return;
		testing = true;
		testError = null;
		try {
			const secrets = buildSecrets(drafts);
			result = await sources.test({ kind: KIND, config: buildConfig(role, values), ...(secrets ? { secrets } : {}), ...(connection ? { id: connection.id } : {}) });
		} catch (err) {
			testError = errorText(err);
		} finally {
			testing = false;
		}
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		problem = missing();
		if (problem) return;
		busy = true;
		error = null;
		try {
			const secrets = buildSecrets(drafts);
			const saved = await sources.save({ name: name.trim(), kind: KIND, config: buildConfig(role, values), ...(secrets ? { secrets } : {}), notes: notes.trim() || null }, connection?.id);
			toast(m.sources_saved({ name: saved.name }), 'signal');
			onclose();
			onsaved?.(saved);
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={flowing ? m.provision_title() : connection ? m.sources_dialog_edit({ name: connection.name }) : m.sources_dialog_add()} {onclose} wide>
	{#if flowing}
		<ProvisionPanel
			start={() => sources.provision({ cloud, ...(name.trim() ? { name: name.trim() } : {}) })}
			oncancel={() => (flowing = null)}
			onmanual={() => {
				flowing = null;
				manualOpen = true;
			}}
			{onclose}
		/>
	{:else}
	<form id="source-form" class="flex flex-col gap-3" onsubmit={submit}>
		<Field label={m.sources_name()} hint={canProvision ? m.provision_name_hint() : undefined}>
			<input class="input" required={!canProvision || manualOpen} bind:value={name} data-testid="source-name" />
		</Field>
		{#if canProvision}
			<div class="flex flex-wrap items-end gap-3 rounded-card border border-line p-3" data-testid="provision-start">
				<div class="min-w-0 flex-1 basis-56">
					<p class="m-0 text-xs text-ink-2">{m.provision_connect_hint()}</p>
				</div>
				<Field label={m.provision_cloud()}>
					<select class="input" bind:value={cloud} data-testid="provision-cloud">
						{#each clouds as option (option)}<option value={option}>{option}</option>{/each}
					</select>
				</Field>
				<Button variant="primary" icon={Cloud} onclick={() => (flowing = 'provision')} data-testid="provision-connect">{m.provision_connect()}</Button>
			</div>
		{/if}
		{#snippet fields()}
			{#if ready && role}
				<ConnectorForm {role} bind:values bind:drafts {hasSecret} />
			{/if}
		{/snippet}
		{#if canProvision}
			<Disclosure title={m.provision_manual()} bind:open={manualOpen} data-testid="provision-manual-details">{@render fields()}</Disclosure>
		{:else}
			{@render fields()}
		{/if}
		{#if locked}<Notice><p class="m-0">{m.sources_locked()}</p></Notice>{/if}
		<Field label={m.sources_notes()}>
			<textarea class="input" rows="2" bind:value={notes}></textarea>
		</Field>
		<p class="m-0 text-xs text-ink-3">{m.sources_help()} <a href={docsUrl('exchange')} target="_blank" rel="noopener noreferrer">{m.sources_help_link()}</a></p>
		<div class="flex flex-wrap items-center gap-2">
			<Button icon={FlaskConical} loading={testing} onclick={test} data-testid="source-test">{m.sources_test_button()}</Button>
		</div>
		{#if result}<CheckList {result} />{/if}
		{#if testError}<p class="m-0 text-xs text-warning-ink" role="alert">{testError}</p>{/if}
		{#if problem}<p class="m-0 text-xs text-warning-ink" role="alert" data-testid="source-problem">{problem}</p>{/if}
		{#if error}<ErrorNotice title={m.sources_save_failed()} message={error} />{/if}
	</form>
	{/if}
	{#snippet actions()}
		{#if !flowing}
			<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
			<Button variant="primary" type="submit" form="source-form" loading={busy} data-testid="source-save">{m.common_save()}</Button>
		{/if}
	{/snippet}
</Dialog>
