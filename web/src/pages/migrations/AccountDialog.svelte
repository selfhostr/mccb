<script lang="ts">
	/**
	 * Edits a mailbox: the destination password, the login and the mailbox's rules. New mailboxes come
	 * from NewMigrationDialog. The password is never sent back: empty keeps it. The login is sent only
	 * when it changed: for the server a new login is a new target, and everything is imported again.
	 */
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, put } from '../../lib/api/client.ts';
	import type { Account } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import PolicyFields from '../settings/PolicyFields.svelte';

	interface Props {
		account: Account;
		onclose: () => void;
	}

	let { account, onclose }: Props = $props();

	// The form starts from the props once: later changes of the props do not rewrite what is typed.
	let password = $state('');
	const initialLogin = untrack(() => (account.login && account.login !== account.email ? account.login : ''));
	let login = $state(initialLogin);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let policyError = $state<string | null>(null);
	let policy = $state<PolicyFields>();

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		policyError = null;
		try {
			// The rules first: a refusal leaves everything as it was.
			if (policy?.dirty()) {
				try {
					await put(`/accounts/${account.id}/policy`, policy.body());
				} catch (err) {
					policyError = errorText(err);
					return;
				}
			}
			const saved = await put<Account>(`/accounts/${account.id}`, { ...(password ? { password } : {}), ...(login !== initialLogin ? { login } : {}) });
			data.upsertAccount(saved);
			toast(m.migrations_account_saved(), 'signal');
			if (password) data.test([saved.id]);
			onclose();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.migrations_account_edit_title({ email: prefs.email(account.email) })} {onclose}>
	<form id="account-form" class="flex flex-col gap-5" onsubmit={submit}>
		<Field label={m.migrations_account_password()} hint={m.migrations_account_password_keep()}>
			<input class="input" type="password" autocomplete="new-password" bind:value={password} />
		</Field>
		<Field label={m.migrations_account_login()} hint={m.migrations_account_login_hint()}>
			<input class="input font-mono" autocomplete="off" bind:value={login} />
		</Field>
		<PolicyFields bind:this={policy} url={`/accounts/${account.id}/policy`} server={false} />
		{#if policyError}<ErrorNotice title={m.policy_save_failed()} message={policyError} />{/if}
		{#if error}<ErrorNotice title={m.migrations_account_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="account-form" loading={busy}>{m.common_save()}</Button>
	{/snippet}
</Dialog>
