<script lang="ts">
	import LogIn from '@lucide/svelte/icons/log-in';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, get, post } from '../lib/api/client.ts';
	import Button from '../lib/ui/Button.svelte';
	import ErrorNotice from '../lib/ui/ErrorNotice.svelte';
	import Field from '../lib/ui/Field.svelte';
	import LanguageSelect from '../lib/ui/LanguageSelect.svelte';
	import Notice from '../lib/ui/Notice.svelte';

	let { expired = false, onlogin }: { expired?: boolean; onlogin: () => unknown } = $props();

	let password = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let input = $state<HTMLInputElement>();
	let version = $state('');

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			await post('/login', { password }, { noAuthRedirect: true });
			await onlogin();
		} catch (err) {
			error = err instanceof ApiError && err.status === 401 ? m.login_form_wrong() : errorText(err);
			input?.select();
		} finally {
			busy = false;
		}
	}

	onMount(() => {
		input?.focus();
		get<{ version: string }>('/version', { noAuthRedirect: true }).then((r) => (version = r.version), () => {});
	});
</script>

<main id="main" class="flex min-h-dvh items-center justify-center p-4">
	<form class="flex w-full max-w-[360px] flex-col gap-4" onsubmit={submit}>
		<div class="flex items-center gap-3">
			<img src="/favicon.svg" alt="" width="32" height="32" />
			<h1 class="text-xl">mccb</h1>
		</div>
		<p class="m-0 text-ink-2">{m.login_form_sentence()}</p>
		{#if expired}<Notice>{m.login_form_expired()}</Notice>{/if}
		<Field label={m.login_form_password()}>
			<input bind:this={input} class="input" type="password" autocomplete="current-password" required bind:value={password} data-testid="login-password" />
		</Field>
		{#if error}<ErrorNotice title={m.login_form_failed()} message={error} />{/if}
		<Button variant="primary" type="submit" icon={LogIn} loading={busy}>{m.login_form_submit()}</Button>
		<div class="flex items-center justify-between gap-3 text-ink-3">
			<LanguageSelect />
			{#if version}<span class="font-mono text-xs">{version}</span>{/if}
		</div>
	</form>
</main>
