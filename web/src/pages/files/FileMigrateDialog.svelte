<script lang="ts">
	/**
	 * A file (PST, mbox, EML) to one or more mailboxes: the destination server, then one row per
	 * mailbox. Each row creates the mailbox and links the file to it; a row that was added is never
	 * sent again, and a row whose mailbox exists but whose link failed retries only the link.
	 * Mailboxes already on the server without a source can take the file as they are.
	 */
	import Check from '@lucide/svelte/icons/check';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import Hourglass from '@lucide/svelte/icons/hourglass';
	import Plus from '@lucide/svelte/icons/plus';
	import ServerIcon from '@lucide/svelte/icons/server';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { Account, Pst } from '../../lib/api/types.ts';
	import { providerOf } from '../../lib/logic/providers.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import Toggle from '../../lib/ui/Toggle.svelte';
	import { roving, tabStop, tileClass } from '../../lib/ui/tiles.ts';

	interface Props {
		pst: Pst;
		onclose: () => void;
	}

	let { pst, onclose }: Props = $props();

	interface Row {
		key: number;
		email: string;
		password: string;
		login: string;
		/** Set once the mailbox exists: a retry then only links the file. */
		accountId: number | null;
		/** A mailbox that was already in the app: only the file is linked. */
		existing: boolean;
		hasPassword: boolean;
		status: 'idle' | 'busy' | 'added' | 'failed';
		error: string | null;
		linkFailed: boolean;
	}

	let nextKey = 1;
	const newRow = (): Row => ({ key: nextKey++, email: '', password: '', login: '', accountId: null, existing: false, hasPassword: false, status: 'idle', error: null, linkFailed: false });

	let serverId = $state(untrack(() => data.servers[0]?.id ?? 0));
	let rows = $state<Row[]>([newRow()]);
	let destLogin = $state(false);
	let busy = $state(false);

	const name = $derived(prefs.file(pst.filename));
	const live = $derived(data.psts.get(pst.id));
	const valid = $derived(live?.status === 'valid');
	const targetName = $derived(data.servers.find((s) => s.id === serverId)?.name ?? '');
	const blank = (r: Row) => !r.email.trim() && !r.password && !r.login.trim();
	/** The destination cannot change once a mailbox of it exists or is listed. */
	const lockedTo = $derived(rows.some((r) => r.accountId != null));
	/** Mailboxes already on the server that have no source yet, not listed below. */
	const candidates = $derived(
		[...data.accounts.values()]
			.filter((a) => a.server_id === serverId && a.pst_id == null && (!a.source || a.source.kind === 'pst') && !rows.some((r) => r.accountId === a.id))
			.sort((a, b) => a.email.localeCompare(b.email))
	);
	const pending = $derived(rows.filter((r) => r.status !== 'added' && !blank(r)).length);
	const addedCount = $derived(rows.filter((r) => r.status === 'added').length);
	const failedCount = $derived(rows.filter((r) => r.status === 'failed').length);

	function addExisting(list: Account[]) {
		const made = list.map((a) => {
			const row = newRow();
			row.accountId = a.id;
			row.existing = true;
			row.hasPassword = a.has_password;
			row.email = a.email;
			row.login = a.login ?? '';
			return row;
		});
		rows = [...rows.filter((r) => r.accountId != null || !blank(r)), ...made];
	}

	function removeRow(row: Row) {
		rows = rows.filter((r) => r.key !== row.key);
		if (!rows.length) rows = [newRow()];
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !valid || !serverId) return;
		const todo = rows.filter((r) => r.status !== 'added' && !blank(r));
		if (!todo.length) return;
		busy = true;
		const added: number[] = [];
		for (const row of todo) {
			row.status = 'busy';
			row.error = null;
			row.linkFailed = false;
			try {
				let id = row.accountId;
				if (row.existing && row.password) data.upsertAccount(await put<Account>(`/accounts/${id}`, { password: row.password }));
				if (id == null) {
					const login = row.login.trim();
					const saved = await post<Account>('/accounts', { server_id: serverId, email: row.email.trim(), password: row.password, ...(destLogin && login ? { login } : {}) });
					data.upsertAccount(saved);
					id = saved.id;
					row.accountId = id;
				}
				try {
					data.upsertAccount(await put<Account>(`/accounts/${id}/pst`, { pst_id: pst.id }));
				} catch (err) {
					row.linkFailed = true;
					throw err;
				}
				row.status = 'added';
				added.push(id);
			} catch (err) {
				row.status = 'failed';
				row.error = errorText(err);
			}
		}
		busy = false;
		// The destination passwords are checked at once: the list shows which mailboxes answer.
		const fresh = added.filter((id) => !rows.find((r) => r.accountId === id)?.existing);
		if (fresh.length) data.test(fresh);
		if (rows.some((r) => r.status === 'failed')) return;
		const done = rows.filter((r) => r.status === 'added').map((r) => r.accountId as number);
		// Read before closing: the parent drops the file with the dialog, and `name` follows it.
		const fileName = name;
		onclose();
		if (done.length === 1) {
			toast(m.files_migrate_done_one({ name: fileName }), 'signal');
			// One mailbox goes on in its panel, on the Migrations page: that is where it starts.
			router.go(`/?account=${done[0]}`);
		} else {
			// Several come back selected: the selection bar offers to test and start them.
			data.selected.clear();
			for (const id of done) data.selected.add(id);
			toast(m.files_migrate_done({ count: done.length, name: fileName }), 'signal');
			router.go('/');
		}
	}
</script>

<Dialog title={m.files_migrate_title({ name })} {onclose} wide>
	{#if !data.servers.length}
		<EmptyState icon={ServerIcon} title={m.migrations_new_no_server()}>
			{#snippet action()}
				<Button variant="primary" icon={Plus} href="/servers" onclick={onclose} data-testid="migrate-add-server">{m.settings_servers_add()}</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<form id="file-migrate-form" class="flex flex-col gap-6" onsubmit={submit} data-testid="file-migrate">
			{#if !valid}<Notice>{m.files_migrate_invalid()}</Notice>{/if}

			<fieldset class="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
				<legend class="mb-2 p-0 text-sm font-semibold">{m.files_migrate_dest()}</legend>
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" tabindex="-1" aria-label={m.files_migrate_dest()} onkeydown={roving} data-testid="migrate-servers">
					{#each data.servers as item, i (item.id)}
						{@const on = serverId === item.id}
						<button type="button" role="radio" aria-checked={on} tabindex={tabStop(on, i, data.servers.some((s) => s.id === serverId))} class={tileClass(on)} disabled={lockedTo || busy} onclick={() => (serverId = item.id)}>
							{#if on}<Check size={14} class="absolute top-1 end-1 text-ink" aria-hidden="true" />{/if}
							<KindIcon icon={providerOf(item.imap_host).icon} mark={providerOf(item.imap_host).mark} size={28} />
							<span class="flex min-w-0 flex-col">
								<span class="truncate text-xs font-medium leading-tight">{item.name}</span>
								<span class="truncate font-mono text-xs text-ink-3">{prefs.host(item.imap_host)}</span>
							</span>
						</button>
					{/each}
				</div>
				<a class="inline-flex min-h-10 items-center gap-2 self-start text-ink-2 no-underline hover:text-ink" href="/servers" onclick={onclose}><Plus size={14} />{m.migrations_new_add_server()}</a>
			</fieldset>

			<fieldset class="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
				<legend class="mb-2 p-0 text-sm font-semibold">{m.migrations_new_mailboxes()}</legend>
				<Toggle checked={destLogin} label={m.migrations_new_login_toggle()} onchange={(on) => (destLogin = on)} />

				<div class="flex flex-col" data-testid="migrate-rows">
					<div class={`hidden gap-2 pb-1 text-xs font-medium text-ink-2 sm:grid ${destLogin ? 'grid-cols-[repeat(3,minmax(0,1fr))_2.5rem]' : 'grid-cols-[repeat(2,minmax(0,1fr))_2.5rem]'}`} aria-hidden="true">
						<span class="truncate">{m.migrations_new_dest_email()}</span>
						<span class="truncate">{m.migrations_new_dest_password()}</span>
						{#if destLogin}<span class="truncate">{m.migrations_new_dest_login()}</span>{/if}
						<span></span>
					</div>
					{#each rows as row, i (row.key)}
						{@const done = row.status === 'added'}
						{@const filled = !blank(row)}
						{@const locked = done || busy || row.accountId != null}
						<div class={`flex flex-col gap-1 py-2 ${i ? 'border-t border-line' : ''}`} data-testid="migrate-row" data-status={row.status}>
							<div class={`grid gap-2 ${destLogin ? 'grid-cols-[minmax(0,1fr)_2.5rem] sm:grid-cols-[repeat(3,minmax(0,1fr))_2.5rem]' : 'grid-cols-[minmax(0,1fr)_2.5rem] sm:grid-cols-[repeat(2,minmax(0,1fr))_2.5rem]'}`}>
								<input class="input font-mono sm:placeholder:text-transparent" type="email" aria-label={m.migrations_new_dest_email()} placeholder={m.migrations_new_dest_email()} autocomplete="off" required={filled} disabled={locked} bind:value={row.email} />
								<input class="input max-sm:col-start-1 sm:placeholder:text-transparent" type="password" aria-label={m.migrations_new_dest_password()} placeholder={row.existing && row.hasPassword ? m.migrations_new_dest_saved() : m.migrations_new_dest_password()} autocomplete="new-password" required={filled && !row.existing} disabled={done || busy || (row.accountId != null && !row.existing) || (row.existing && row.hasPassword)} bind:value={row.password} />
								{#if destLogin}
									<input class="input max-sm:col-start-1 font-mono sm:placeholder:text-transparent" aria-label={m.migrations_new_dest_login()} placeholder={m.migrations_new_dest_login()} autocomplete="off" disabled={locked} bind:value={row.login} />
								{/if}
								<div class="flex w-10 max-sm:col-start-2 max-sm:row-start-1">
									{#if !done && (row.accountId == null || row.existing)}
										<IconButton icon={Trash2} label={m.migrations_new_remove_row()} disabled={busy} onclick={() => removeRow(row)} />
									{/if}
								</div>
							</div>
							{#if row.status !== 'idle'}
								<div class="flex min-w-0 flex-wrap items-baseline gap-x-2">
									{#if row.status === 'busy'}
										<Plate icon={Hourglass} label={m.migrations_new_row_adding()} />
									{:else if done}
										<Plate tone="signal" icon={CircleCheck} label={m.migrations_new_row_added()} />
									{:else}
										<Plate tone="warning" icon={CircleX} label={m.migrations_new_row_failed()} />
										<span class="min-w-0 text-xs break-words text-ink-2">{row.linkFailed ? `${m.files_migrate_link_failed()} ${row.error ?? ''}` : row.error}</span>
									{/if}
								</div>
							{/if}
						</div>
					{/each}
				</div>

				<div>
					<Button icon={Plus} onclick={() => (rows = [...rows, newRow()])} disabled={busy} data-testid="migrate-row-add">{m.migrations_add_account()}</Button>
				</div>

				{#if candidates.length}
					<Disclosure title={m.migrations_new_existing({ count: candidates.length })} data-testid="migrate-existing">
						<div class="flex flex-col gap-1">
							<p class="m-0 text-xs text-ink-2">{m.files_migrate_existing_hint({ server: targetName })}</p>
							<ul class="m-0 flex list-none flex-col p-0">
								{#each candidates as a (a.id)}
									<li class="flex min-h-10 items-center gap-2">
										<span class="min-w-0 flex-1 truncate font-mono text-xs">{prefs.email(a.email)}</span>
										<Button variant="ghost" icon={Plus} onclick={() => addExisting([a])} disabled={busy}>{m.migrations_new_existing_use()}</Button>
									</li>
								{/each}
							</ul>
							{#if candidates.length > 1}<div><Button onclick={() => addExisting(candidates)} disabled={busy}>{m.migrations_new_existing_all()}</Button></div>{/if}
						</div>
					</Disclosure>
				{/if}

				{#if !busy && addedCount && failedCount}
					<Notice>{m.migrations_new_partial({ added: addedCount, failed: failedCount })}</Notice>
				{/if}
			</fieldset>
		</form>
	{/if}
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		{#if data.servers.length}
			<Button variant="primary" type="submit" form="file-migrate-form" loading={busy} disabled={!valid || !pending || !serverId} data-testid="migrate-submit">
				{pending > 1 ? m.migrations_new_submit_many({ count: pending }) : m.migrations_new_submit_one()}
			</Button>
		{/if}
	{/snippet}
</Dialog>
