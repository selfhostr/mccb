<script lang="ts">
	/**
	 * Add a mailbox by hand (one list for the Repair and Duplicates tabs), or change its password and login.
	 * Tested right after it is saved. `oncsv` offers the CSV import instead; `onchange` runs after a save and a test.
	 */
	import FileUp from '@lucide/svelte/icons/file-up';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { RepairBox } from '../../lib/api/types.ts';
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
	const initialLogin = untrack(() => (box?.login && box.login !== box.email ? box.login : ''));
	let serverId = $state(initialServer);
	let email = $state('');
	let password = $state('');
	let login = $state(initialLogin);
	let busy = $state(false);
	let error = $state<string | null>(null);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			const saved = box
				? await put<RepairBox>(`/repair/boxes/${box.id}`, { ...(password ? { password } : {}), ...(login !== initialLogin ? { login } : {}) })
				: await post<RepairBox>('/repair/boxes', { server_id: Number(serverId), email: email.trim(), password, ...(login ? { login } : {}) });
			toast(m.migrations_account_saved(), 'signal');
			onclose();
			if (!box || password || login !== initialLogin) void repairs.test([saved.id]).then(() => onchange?.());
			else await repairs.refresh();
			onchange?.();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={box ? m.migrations_account_edit_title({ email: prefs.email(box.email) }) : m.repair_add()} {onclose}>
	<form id="repair-box-form" class="flex flex-col gap-3" onsubmit={submit}>
		{#if !box}
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
		<Field label={m.migrations_account_password()} hint={box ? m.migrations_account_password_keep() : undefined}>
			<input class="input" type="password" required={!box} autocomplete="new-password" bind:value={password} data-testid="repair-box-password" />
		</Field>
		<Field label={m.migrations_account_login()} hint={m.migrations_account_login_hint()}>
			<input class="input font-mono" autocomplete="off" bind:value={login} />
		</Field>
		{#if error}<ErrorNotice title={m.migrations_account_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		{#if !box && oncsv}<span class="mr-auto"><Button variant="ghost" icon={FileUp} onclick={oncsv} data-testid="repair-csv">{m.migrations_add_csv()}</Button></span>{/if}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="repair-box-form" loading={busy} data-testid="repair-box-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
