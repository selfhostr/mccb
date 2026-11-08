<script lang="ts">
	/** Adds or edits a Microsoft 365 tenant: connection-scoped fields of the catalogue, a Test before saving. */
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
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
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ConnectorForm from '../migrations/ConnectorForm.svelte';
	import CheckList from './CheckList.svelte';

	const KIND = 'm365_graph';
	interface Props {
		connection?: SourceConnection | null;
		onclose: () => void;
		onsaved?: (saved: SourceConnection) => void;
	}
	let { connection = null, onclose, onsaved }: Props = $props();

	const full = $derived(connectors.list.find((c) => c.kind === KIND)?.source ?? null);
	const role = $derived<ConnectorRole | null>(full ? { ...full, fields: full.fields.filter((f) => f.scope === 'connection') } : null);

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

<Dialog title={connection ? m.sources_dialog_edit({ name: connection.name }) : m.sources_dialog_add()} {onclose} wide>
	<form id="source-form" class="flex flex-col gap-3" onsubmit={submit}>
		<Field label={m.sources_name()}>
			<input class="input" required bind:value={name} data-testid="source-name" />
		</Field>
		{#if ready && role}
			<ConnectorForm {role} bind:values bind:drafts {hasSecret} />
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
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="source-form" loading={busy} data-testid="source-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
