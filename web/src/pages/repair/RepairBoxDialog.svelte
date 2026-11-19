<script lang="ts">
	/**
	 * Add a mailbox by hand (one list for the Repair and Duplicates tabs), or change its password and login.
	 * Tested right after it is saved, and the dialog says how the test went: it closes when the connection works and
	 * stays open, with the reason, when it does not. `oncsv` offers the CSV import instead; `onchange` runs after a save and a test.
	 */
	import FileUp from '@lucide/svelte/icons/file-up';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { RepairBox } from '../../lib/api/types.ts';
	import { connectionOf } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { repairs } from '../../lib/stores/repair.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	type Editable = Pick<RepairBox, 'id' | 'server_id' | 'email' | 'login'>;

	let { box = null, oncsv, onchange, onclose }: { box?: Editable | null; oncsv?: () => void; onchange?: () => void; onclose: () => void } = $props();

	const initialServer = untrack(() => box?.server_id ?? data.servers[0]?.id ?? 0);
	let savedLogin = untrack(() => (box?.login && box.login !== box.email ? box.login : ''));
	/** The mailbox as saved: the dialog's own form edits it once it exists, also right after an add. */
	let current = $state.raw<Editable | null>(untrack(() => box));
	let serverId = $state(initialServer);
	let email = $state('');
	let password = $state('');
	let login = $state(savedLogin);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let testFailure = $state<string | null>(null);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		testFailure = null;
		try {
			const saved = current
				? await put<RepairBox>(`/repair/boxes/${current.id}`, { ...(password ? { password } : {}), ...(login !== savedLogin ? { login } : {}) })
				: await post<RepairBox>('/repair/boxes', { server_id: Number(serverId), email: email.trim(), password, ...(login ? { login } : {}) });
			toast(m.migrations_account_saved(), 'signal');
			const retest = !current || !!password || login !== savedLogin;
			current = saved;
			savedLogin = login;
			password = '';
			if (!retest) {
				await repairs.refresh();
				onchange?.();
				onclose();
				return;
			}
			await repairs.test([saved.id]);
			onchange?.();
			const tested = repairs.boxes.find((b) => b.id === saved.id);
			if (tested && connectionOf(tested) === 'ok') {
				toast(m.repair_box_test_ok({ email: prefs.email(saved.email) }), 'signal');
				onclose();
			} else if (tested) {
				const result = tested.last_test_result;
				testFailure = result?.imap?.msg || result?.caldav?.msg || result?.carddav?.msg || (tested.status === 'auth_failed' ? m.migrations_row_login_refused() : m.migrations_test_status_failed());
			}
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={current ? m.migrations_account_edit_title({ email: prefs.email(current.email) }) : m.repair_add()} {onclose}>
	<form id="repair-box-form" class="flex flex-col gap-3" onsubmit={submit}>
		{#if !current}
			<Field label={m.repair_box_server()}>
				<select class="input" bind:value={serverId} data-testid="repair-box-server">
					{#each data.servers as server (server.id)}
						<option value={server.id}>{server.name}</option>
					{/each}
				</select>
			</Field>
			<Field label={m.migrations_account_email()}>
				<input class="input font-mono" type="email" required autocomplete="off" bind:value={email} data-testid="repair-box-email" />
			</Field>
		{/if}
		<Field label={m.migrations_account_password()} hint={current ? m.migrations_account_password_keep() : undefined}>
			<input class="input" type="password" required={!current} autocomplete="new-password" bind:value={password} data-testid="repair-box-password" />
		</Field>
		<Field label={m.migrations_account_login()} hint={m.migrations_account_login_hint()}>
			<input class="input font-mono" autocomplete="off" bind:value={login} />
		</Field>
		{#if testFailure}<ErrorNotice title={m.repair_box_saved_test_failed()} message={testFailure} />{/if}
		{#if error}<ErrorNotice title={m.migrations_account_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		{#if !current && oncsv}<span class="mr-auto"><Button variant="ghost" icon={FileUp} onclick={oncsv} data-testid="repair-csv">{m.migrations_add_csv()}</Button></span>{/if}
		<Button variant="ghost" onclick={onclose}>{testFailure ? m.common_close() : m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="repair-box-form" loading={busy} data-testid="repair-box-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
