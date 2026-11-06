<script lang="ts">
	/** The server downloads a PST itself: from a URL, over SFTP, or from S3-compatible storage. */
	import Download from '@lucide/svelte/icons/download';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { FetchKind, Pst } from '../../lib/api/types.ts';
	import { FETCH_FIELDS, FETCH_KINDS, type FetchField, type FetchFieldName, type FetchValues, buildFetchBody, missingFetchFields } from '../../lib/logic/fetch.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import SecretInput from '../../lib/ui/SecretInput.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	let { onclose }: { onclose: () => void } = $props();

	const KIND_NAMES: Record<FetchKind, string> = { http: 'HTTP(S)', sftp: 'SFTP', s3: 'S3' };

	let kind = $state<FetchKind>('http');
	let values = $state<FetchValues>({});
	let busy = $state(false);
	let error = $state<string | null>(null);
	let missing = $state<FetchFieldName[]>([]);

	const LABELS = $derived<Record<FetchFieldName, string>>({
		url: m.files_fetch_url(),
		token: m.files_fetch_token(),
		username: m.files_fetch_username(),
		password: m.files_fetch_password(),
		filename: m.files_fetch_filename(),
		host: m.files_fetch_host(),
		port: m.files_fetch_port(),
		path: m.files_fetch_path(),
		private_key: m.files_fetch_private_key(),
		passphrase: m.files_fetch_passphrase(),
		host_key_sha256: m.files_fetch_host_key(),
		endpoint: m.files_fetch_endpoint(),
		bucket: m.files_fetch_bucket(),
		key: m.files_fetch_key(),
		region: m.files_fetch_region(),
		access_key: m.files_fetch_access_key(),
		secret_key: m.files_fetch_secret_key()
	});
	const HINTS: Partial<Record<FetchFieldName, () => string>> = {
		host_key_sha256: () => m.files_fetch_host_key_hint(),
		filename: () => m.files_fetch_filename_hint()
	};

	const main = $derived(FETCH_FIELDS[kind].filter((f) => !f.advanced));
	const more = $derived(FETCH_FIELDS[kind].filter((f) => f.advanced));

	function pick(next: FetchKind) {
		kind = next;
		values = {};
		missing = [];
		error = null;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		missing = missingFetchFields(kind, values);
		if (missing.length) {
			error = m.files_fetch_missing({ fields: [...new Set(missing)].map((n) => LABELS[n]).join(', ') });
			return;
		}
		busy = true;
		error = null;
		try {
			const pst = await post<Pst>('/psts/fetch', buildFetchBody(kind, values));
			data.upsertPst(pst);
			toast(m.files_fetch_started(), 'signal');
			onclose();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

{#snippet input(f: FetchField)}
	{#if f.secret}
		<SecretInput label={LABELS[f.name]} hasSecret={false} area={f.area} hint={HINTS[f.name]?.()} bind:value={values[f.name]} data-field={f.name} />
	{:else}
		<Field label={LABELS[f.name]} hint={HINTS[f.name]?.()}>
			<input class="input font-mono" type={f.number ? 'number' : 'text'} autocomplete="off" placeholder={f.placeholder} bind:value={values[f.name]} data-field={f.name} />
		</Field>
	{/if}
{/snippet}

<Dialog title={m.files_fetch_title()} {onclose} wide>
	<form id="fetch-form" class="flex flex-col gap-3" onsubmit={submit}>
		<p class="m-0 text-ink-2">{m.files_fetch_sentence()}</p>
		<Field label={m.files_fetch_kind()}>
			<select class="input" value={kind} onchange={(e) => pick(e.currentTarget.value as FetchKind)} data-testid="fetch-kind">
				{#each FETCH_KINDS as k (k)}
					<option value={k}>{KIND_NAMES[k]}</option>
				{/each}
			</select>
		</Field>
		{#each main as f (`${kind}-${f.name}`)}
			{@render input(f)}
		{/each}
		{#if more.length}
			<Disclosure title={m.files_fetch_more()}>
				<div class="flex flex-col gap-3">
					{#each more as f (`${kind}-${f.name}`)}
						{@render input(f)}
					{/each}
				</div>
			</Disclosure>
		{/if}
		{#if error}<ErrorNotice title={m.files_fetch_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" icon={Download} type="submit" form="fetch-form" loading={busy} data-testid="fetch-submit">{m.files_fetch_start()}</Button>
	{/snippet}
</Dialog>
