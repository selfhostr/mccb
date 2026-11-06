<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, post } from '../../lib/api/client.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	let current = $state('');
	let next = $state('');
	let repeat = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		error = null;
		if (next !== repeat) {
			error = m.settings_password_mismatch();
			return;
		}
		busy = true;
		try {
			await post('/password', { current, new: next }, { noAuthRedirect: true });
			current = next = repeat = '';
			toast(m.settings_password_done(), 'signal');
		} catch (err) {
			const status = err instanceof ApiError ? err.status : 0;
			if (status === 409) error = m.settings_password_env();
			else if (status === 401 || status === 403) error = m.settings_password_wrong();
			else error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Section id="password" title={m.settings_password_title()} sentence={m.settings_password_sentence()}>
	<form class="flex max-w-[460px] flex-col gap-3" onsubmit={submit}>
		<Field label={m.settings_password_current()}>
			<input class="input" type="password" autocomplete="current-password" required bind:value={current} />
		</Field>
		<Field label={m.settings_password_new()} hint={m.settings_password_hint()}>
			<input class="input" type="password" autocomplete="new-password" required minlength="8" maxlength="256" bind:value={next} />
		</Field>
		<Field label={m.settings_password_repeat()}>
			<input class="input" type="password" autocomplete="new-password" required bind:value={repeat} />
		</Field>
		{#if error}<ErrorNotice title={m.settings_password_failed()} message={error} />{/if}
		<div><Button type="submit" loading={busy}>{m.settings_password_submit()}</Button></div>
	</form>
</Section>
