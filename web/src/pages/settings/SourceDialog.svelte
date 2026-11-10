<script lang="ts">
	/**
	 * Adds or edits a server that is not a plain IMAP row. A Microsoft 365 tenant has its own fields and a
	 * Test before saving. Any other kind (JMAP, Exchange...) keeps here the settings its mailboxes share:
	 * it is checked with each mailbox, not here.
	 */
	import ArrowLeft from '@lucide/svelte/icons/arrow-left';
	import ExternalLink from '@lucide/svelte/icons/external-link';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Cloud from '@lucide/svelte/icons/cloud';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import { docsUrl } from '../../lib/base.ts';
	import type { CheckResult, ConnectorRole, SourceConnection } from '../../lib/api/types.ts';
	import { buildConfig, buildSecrets, initialSecrets, initialValues, missingRequired, type SecretDrafts, type Values } from '../../lib/logic/connector.ts';
	import { sharedRole, type ServerPreset } from '../../lib/logic/serverKinds.ts';
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

	interface Props {
		connection?: SourceConnection | null;
		/** Adding: the connector kind. Editing: the kind of the connection. */
		kind?: string;
		preset?: ServerPreset;
		onclose: () => void;
		onback?: () => void;
		onsaved?: (saved: SourceConnection) => void;
	}
	let { connection = null, kind = 'm365_graph', preset, onclose, onback, onsaved }: Props = $props();

	const KIND = untrack(() => connection?.kind ?? kind);
	const spec = $derived(connectors.list.find((c) => c.kind === KIND));
	const full = $derived(spec?.source ?? null);
	// A tenant lists its mailboxes and is tested here; the other kinds only hold shared settings.
	const directory = $derived(spec?.directory === true);
	const canProvision = $derived(!connection && directory && spec?.provision === true);
	// The labels and helps of the connection fields are served in English: the dialog says them in the language of the UI.
	const TEXTS: Record<string, () => { label: string; help?: string }> = {
		tenant_id: () => ({ label: m.provision_field_tenant_id(), help: m.provision_field_tenant_id_help() }),
		client_id: () => ({ label: m.provision_field_client_id(), help: m.provision_field_client_id_help() }),
		client_secret: () => ({ label: m.provision_field_client_secret(), help: m.provision_field_client_secret_help() }),
		cloud: () => ({ label: m.provision_field_cloud(), help: m.provision_field_cloud_help() })
	};
	let values = $state<Values>({});
	const role = $derived<ConnectorRole | null>(
		!full ? null : directory ? { ...full, fields: full.fields.filter((f) => f.scope === 'connection').map((f) => ({ ...f, ...(TEXTS[f.name]?.() ?? {}) })) } : sharedRole(full, KIND, values)
	);
	const clouds = $derived(full?.fields.find((f) => f.name === 'cloud')?.options ?? ['global']);

	// "Connect with Microsoft": the flow replaces the form while it runs.
	let flowing = $state<'provision' | null>(null);
	let manualOpen = $state(false);
	let cloud = $state('global');
	$effect(() => {
		if (!canProvision) manualOpen = true;
	});

	let name = $state(untrack(() => connection?.name ?? preset?.name ?? ''));
	let notes = $state(untrack(() => connection?.notes ?? ''));
	let drafts = $state<SecretDrafts>({});
	let ready = $state(false);
	// The catalogue may arrive after the dialog opens: the form is built once it is there.
	// Built from every field of the kind: which ones show depends on the values (a service account, an auth mode).
	$effect(() => {
		if (!full || ready) return;
		untrack(() => {
			values = initialValues(full, connection?.config ?? preset?.config);
			drafts = initialSecrets(full);
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

	/** Only the secrets of the fields shown; one the server held for a field now hidden is removed. */
	function secrets(): Record<string, string> | undefined {
		const shown = new Set(role?.fields.filter((f) => f.secret).map((f) => f.name));
		const out = buildSecrets(Object.fromEntries(Object.entries(drafts).filter(([field]) => shown.has(field)))) ?? {};
		if (!directory) for (const held of connection?.secret_names ?? []) if (!shown.has(held)) out[held] = '';
		return Object.keys(out).length ? out : undefined;
	}

	async function test() {
		problem = missing();
		if (problem) return;
		testing = true;
		testError = null;
		try {
			const typed = secrets();
			result = await sources.test({ kind: KIND, config: buildConfig(role, values), ...(typed ? { secrets: typed } : {}), ...(connection ? { id: connection.id } : {}) });
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
			const typed = secrets();
			const saved = await sources.save({ name: name.trim(), kind: KIND, config: buildConfig(role, values), ...(typed ? { secrets: typed } : {}), notes: notes.trim() || null }, connection?.id);
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

<Dialog
	title={flowing ? m.provision_title() : connection ? m.sources_dialog_edit({ name: connection.name }) : directory ? m.sources_dialog_add() : m.servers_kind_title({ name: preset?.title ?? spec?.label ?? KIND })}
	{onclose}
	wide
>
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
		{#if !connection && preset?.note}
			<div class="rounded-btn bg-surface-2 p-3 text-sm text-ink-2" data-testid="server-note">
				<p class="m-0">{preset.note}</p>
				{#if preset.link}
					<a class="inline-flex min-h-10 items-center gap-1 font-medium" href={preset.link.href} target="_blank" rel="noopener noreferrer">{preset.link.label}<ExternalLink size={14} /></a>
				{/if}
			</div>
		{/if}
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
		{#if locked}<Notice><p class="m-0">{directory ? m.sources_locked() : m.servers_locked()}</p></Notice>{/if}
		<Field label={m.sources_notes()}>
			<textarea class="input" rows="2" bind:value={notes}></textarea>
		</Field>
		{#if directory}
			<p class="m-0 text-xs text-ink-3">{m.sources_help()} <a href={docsUrl('exchange')} target="_blank" rel="noopener noreferrer">{m.sources_help_link()}</a></p>
			<div class="flex flex-wrap items-center gap-2">
				<Button icon={FlaskConical} loading={testing} onclick={test} data-testid="source-test">{m.sources_test_button()}</Button>
			</div>
			{#if result}<CheckList {result} />{/if}
		{:else if spec}
			<p class="m-0 text-xs text-ink-3" data-testid="server-no-check">{m.servers_no_check()}</p>
		{/if}
		{#if testError}<p class="m-0 text-xs text-warning-ink" role="alert">{testError}</p>{/if}
		{#if problem}<p class="m-0 text-xs text-warning-ink" role="alert" data-testid="source-problem">{problem}</p>{/if}
		{#if error}<ErrorNotice title={directory ? m.sources_save_failed() : m.settings_server_save_failed()} message={error} />{/if}
	</form>
	{/if}
	{#snippet actions()}
		{#if !flowing}
			{#if onback}<span class="mr-auto"><Button variant="ghost" icon={ArrowLeft} onclick={onback}>{m.common_back()}</Button></span>{/if}
			<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
			<Button variant="primary" type="submit" form="source-form" loading={busy} data-testid="source-save">{m.common_save()}</Button>
		{/if}
	{/snippet}
</Dialog>
