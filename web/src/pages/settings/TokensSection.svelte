<script lang="ts">
	/** API tokens for scripts and the command line. A new token is shown once, with a copy button. */
	import Ban from '@lucide/svelte/icons/ban';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import Plus from '@lucide/svelte/icons/plus';
	import Settings from '@lucide/svelte/icons/settings';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { del, errorText, get, post } from '../../lib/api/client.ts';
	import type { ApiToken, CreatedToken } from '../../lib/api/types.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	let tokens = $state.raw<ApiToken[] | null>(null);
	let loadError = $state<string | null>(null);
	let name = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let created = $state.raw<CreatedToken | null>(null);
	let revoking = $state.raw<ApiToken | null>(null);
	let editing = $state(false);
	const active = $derived(tokens?.filter((t) => !t.revoked_at).length ?? 0);

	function close() {
		editing = false;
		created = null;
		error = null;
	}

	async function load() {
		loadError = null;
		try {
			tokens = await get<ApiToken[]>('/tokens');
		} catch (e) {
			loadError = errorText(e);
		}
	}
	onMount(() => void load());

	async function create(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			created = await post<CreatedToken>('/tokens', { name: name.trim() });
			name = '';
			await load();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}

	async function revoke(token: ApiToken) {
		try {
			await del(`/tokens/${token.id}`);
			toast(m.settings_tokens_revoked(), 'signal');
			await load();
		} catch (err) {
			toast(errorText(err), 'warning');
		}
	}
</script>

<div data-testid="section-tokens">
	<SettingRow as="div" label={m.settings_tokens_title()}>
		<span class="text-ink-2" data-testid="token-count">{#if tokens}{active ? m.settings_tokens_count({ count: active }) : m.settings_state_none()}{/if}</span>
		<IconButton icon={Settings} label={m.settings_tokens_manage()} onclick={() => (editing = true)} data-testid="tokens-open" />
	</SettingRow>

	{#if editing}
		<Dialog title={m.settings_tokens_title()} onclose={close}>
			<div class="flex flex-col gap-4">
				<p class="m-0 text-ink-2">{m.settings_tokens_sentence()}</p>
				<form class="flex flex-wrap items-end gap-2" onsubmit={create}>
					<Field label={m.settings_tokens_name()} class="flex-1">
						<input class="input" maxlength="100" autocomplete="off" bind:value={name} data-testid="token-name" />
					</Field>
					<Button type="submit" icon={Plus} loading={busy} data-testid="token-create">{m.settings_tokens_create()}</Button>
				</form>
				{#if error}<ErrorNotice title={m.settings_tokens_create_failed()} message={error} />{/if}
				{#if created}
					<div data-testid="token-created">
						<Notice>
							<p class="m-0 font-medium">{m.settings_tokens_once()}</p>
							<div class="mt-1 flex items-center gap-1">
								<code class="min-w-0 flex-1 rounded-plate bg-surface px-2 py-1 font-mono text-xs break-all" data-testid="token-value">{created.token}</code>
								<CopyButton text={created.token} label={m.settings_tokens_copy()} />
							</div>
							<div class="mt-1"><Button variant="ghost" onclick={() => (created = null)} data-testid="token-hide">{m.settings_tokens_done()}</Button></div>
						</Notice>
					</div>
				{/if}
				<div>
					{#if loadError}
						<ErrorNotice title={m.settings_tokens_load_failed()} message={loadError} onretry={load} />
					{:else if !tokens}
						<Skeleton class="h-12" />
					{:else if !tokens.length}
						<p class="m-0 text-ink-3">{m.settings_tokens_none()}</p>
					{:else}
						<ul class="m-0 flex list-none flex-col divide-y divide-line rounded-card border border-line-strong p-0" data-testid="token-list">
							{#each tokens as token (token.id)}
								<li class="flex min-h-12 items-center gap-2 py-1.5 pr-2 pl-4" data-token={token.name}>
									<div class="min-w-0 flex-1">
										<p class="m-0 flex flex-wrap items-center gap-2">
											<span class="truncate font-medium">{token.name}</span>
											{#if token.revoked_at}<Plate tone="ghost" icon={Ban} label={m.settings_tokens_revoked_state()} />{:else}<Plate tone="signal" icon={CircleCheck} label={m.settings_tokens_active()} />{/if}
										</p>
										<p class="m-0 text-xs text-ink-3">
											<span class="font-mono">{token.prefix}</span>
											<span> {m.settings_tokens_created()} <RelTime value={token.created_at} /></span>
											<span> {m.settings_tokens_last_used()} {#if token.last_used_at}<RelTime value={token.last_used_at} />{:else}{m.settings_tokens_never()}{/if}</span>
										</p>
									</div>
									{#if !token.revoked_at}
										<IconButton icon={Ban} tone="danger" label={m.settings_tokens_revoke_named({ name: token.name })} onclick={() => (revoking = token)} />
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</div>
		</Dialog>
	{/if}
</div>

{#if revoking}
	{@const target = revoking}
	<Confirm title={m.settings_tokens_revoke_title()} text={m.settings_tokens_revoke_text({ name: target.name })} confirm={m.settings_tokens_revoke()} danger onconfirm={() => revoke(target)} onclose={() => (revoking = null)} />
{/if}
