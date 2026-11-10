<script lang="ts">
	import Settings from '@lucide/svelte/icons/settings';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, post } from '../../lib/api/client.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	let current = $state('');
	let next = $state('');
	let repeat = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let editing = $state(false);

	function close() {
		editing = false;
		current = next = repeat = '';
		error = null;
	}

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
			close();
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

<div data-testid="section-password">
	<SettingRow as="div" label={m.settings_password_title()}>
		<IconButton icon={Settings} label={m.settings_password_change()} onclick={() => (editing = true)} data-testid="password-open" />
	</SettingRow>

	{#if editing}
		<Dialog title={m.settings_password_change()} onclose={close}>
			<form id="password-form" class="flex flex-col gap-3" onsubmit={submit}>
				<p class="m-0 text-ink-2">{m.settings_password_sentence()}</p>
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
			</form>
			{#snippet actions()}
				<Button variant="primary" type="submit" form="password-form" loading={busy} data-testid="password-submit">{m.settings_password_submit()}</Button>
			{/snippet}
		</Dialog>
	{/if}
</div>
