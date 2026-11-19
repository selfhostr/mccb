<script lang="ts">
	/**
	 * A new migration: where the mail comes from (a saved server or a connector), the server it goes to, then
	 * one row per mailbox, as many as needed. Servers are typed once on the Servers page: a row only gives
	 * what that mailbox adds (its logins and passwords). Rows are added one after the other; a row that
	 * was added is never sent again, and a row whose mailbox exists but whose source failed retries only
	 * the source. A list (pasted, or a .csv/.txt file) fills the rows with both sides at once, see ListImport. A directory (a Microsoft 365 tenant) lists its mailboxes itself, on its own screen.
	 * A file (PST, mbox, EML) is migrated from the Files page, not from here.
	 */
	import ArrowRight from '@lucide/svelte/icons/arrow-right';
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
	import type { Account, EndpointInput, Server, SourceConnection } from '../../lib/api/types.ts';
	import { isSecret, kindLabel } from '../../lib/logic/connector.ts';
	import type { ListColumn, ListRow } from '../../lib/logic/mailboxList.ts';
	import { kindIcon, providerOf } from '../../lib/logic/providers.ts';
	import { isDirectory, mailboxFields } from '../../lib/logic/serverKinds.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Help from '../../lib/ui/Help.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import Toggle from '../../lib/ui/Toggle.svelte';
	import { roving, tabStop, tileClass } from '../../lib/ui/tiles.ts';
	import ListImport from './ListImport.svelte';

	interface Props {
		defaultServer?: number | null;
		onclose: () => void;
	}

	let { defaultServer = null, onclose }: Props = $props();

	void sources.load();

	type Source = { type: 'none' } | { type: 'server'; server: Server } | { type: 'connection'; connection: SourceConnection; directory: boolean };

	/** One column a row gives on the source side. */
	interface Col {
		name: string;
		label: string;
		secret: boolean;
	}

	interface Row {
		key: number;
		/** Source fields by name. */
		src: Record<string, string>;
		email: string;
		password: string;
		login: string;
		/** Set once the mailbox exists: a retry then only sets the source. */
		accountId: number | null;
		/** A mailbox that was already in the app: its destination stays as saved, only its source is set here. */
		existing: boolean;
		hasPassword: boolean;
		status: 'idle' | 'busy' | 'added' | 'failed';
		error: string | null;
		sourceFailed: boolean;
	}

	let nextKey = 1;
	const newRow = (): Row => ({ key: nextKey++, src: {}, email: '', password: '', login: '', accountId: null, existing: false, hasPassword: false, status: 'idle', error: null, sourceFailed: false });

	// The form starts from the props once: later changes of the props do not rewrite what is typed.
	// The source starts on the first server, the destination on another one when there is one.
	const firstFrom = untrack(() => data.servers.find((s) => s.id !== defaultServer) ?? data.servers[0]);
	let serverId = $state(untrack(() => defaultServer ?? data.servers.find((s) => s.id !== firstFrom?.id)?.id ?? data.servers[0]?.id ?? 0));
	let fromValue = $state(untrack(() => (firstFrom ? `server-${firstFrom.id}` : sources.list[0] ? `connection-${sources.list[0].id}` : '')));
	let rows = $state<Row[]>([newRow()]);
	let destLogin = $state(false);
	let busy = $state(false);
	/** The rows the last list import added, and what it changed, so it can be taken back. */
	let lastImport = $state.raw<{ keys: Set<number>; destLogin: boolean } | null>(null);

	const source = $derived.by((): Source => {
		const [type, id] = fromValue.split('-');
		if (type === 'server') {
			const server = data.servers.find((s) => s.id === Number(id));
			if (server) return { type: 'server', server };
		}
		if (type === 'connection') {
			const connection = sources.list.find((c) => c.id === Number(id));
			if (connection) return { type: 'connection', connection, directory: isDirectory(connectors.list, connection.kind) };
		}
		return { type: 'none' };
	});
	const onDirectory = $derived(source.type === 'connection' && source.directory);
	const sameServer = $derived(source.type === 'server' && source.server.id === Number(serverId));

	const srcCols = $derived.by((): Col[] => {
		if (source.type === 'server') {
			return [
				{ name: 'login', label: m.migrations_new_src_login(), secret: false },
				{ name: 'password', label: m.migrations_new_src_password(), secret: true }
			];
		}
		if (source.type === 'connection' && !source.directory) {
			const role = connectors.list.find((c) => c.kind === source.connection.kind)?.source;
			return role ? mailboxFields(role, source.connection).map((f) => ({ name: f.name, label: f.label || f.name, secret: isSecret(f) })) : [];
		}
		return [];
	});
	const sourceName = $derived(source.type === 'server' ? source.server.name : source.type === 'connection' ? source.connection.name : '');
	const targetName = $derived(data.servers.find((s) => s.id === Number(serverId))?.name ?? '');
	const destCount = $derived(destLogin ? 3 : 2);
	/**
	 * The columns of an imported list, in the order of the form: the source fields, then the destination.
	 * The destination login is always read, the template only has it while the form shows it.
	 */
	const listCols = $derived.by((): (ListColumn & { label: string })[] => [
		...srcCols.map((c) => ({
			key: `source_${c.name}`,
			label: c.label,
			aliases: [c.label, `src_${c.name}`, `old_${c.name}`, ...(c.name === 'login' ? ['source', 'source_email', 'from'] : [])],
			required: true,
			secret: c.secret,
			fallback: !c.secret,
			sample: c.secret ? 'old-secret' : c.name === 'login' ? 'alice@old.example' : 'alice'
		})),
		{ key: 'email', label: m.migrations_new_dest_email(), aliases: [m.migrations_new_dest_email(), 'e-mail', 'mail', 'address', 'adresse', 'destination', 'destination_email', 'dest_email', 'to'], required: true, email: true, sample: 'alice@new.example' },
		{ key: 'password', label: m.migrations_new_dest_password(), aliases: [m.migrations_new_dest_password(), 'destination_password', 'dest_password', 'mot de passe', 'pass', 'pwd'], required: true, secret: true, sample: 'new-secret' },
		{ key: 'login', label: m.migrations_new_dest_login(), aliases: [m.migrations_new_dest_login(), 'destination_login', 'dest_login', 'username', 'user', 'identifiant'], sample: 'alice' }
	]);
	const listShown = $derived(destLogin ? listCols : listCols.filter((c) => c.key !== 'login'));
	/** Addresses a list may not bring twice: those of the rows and of the mailboxes already on the destination. */
	const listTaken = $derived([
		...rows.filter((r) => !blank(r)).map((r) => targetEmail(r).toLowerCase()),
		...[...data.accounts.values()].filter((a) => a.server_id === Number(serverId)).map((a) => a.email.toLowerCase())
	].filter(Boolean));
	const listUndoable = $derived(!!lastImport && !busy && rows.some((r) => lastImport?.keys.has(r.key)) && rows.every((r) => !lastImport?.keys.has(r.key) || r.status === 'idle'));

	const fromTiles = $derived([
		...data.servers.map((s) => ({ value: `server-${s.id}`, name: s.name, sub: prefs.host(s.imap_host), mono: true, icon: providerOf(s.imap_host).icon })),
		...sources.list.map((c) => ({ value: `connection-${c.id}`, name: c.name, sub: kindLabel(connectors.list, c.kind), mono: false, icon: kindIcon(c.kind) }))
	]);

	const value = (r: Row, name: string) => (r.src[name] ?? '').trim();
	const blank = (r: Row) => !r.email.trim() && !r.password && !r.login.trim() && srcCols.every((c) => !value(r, c.name));
	/** A source login that is an address stands for the destination address when that one is empty. */
	const fallback = (r: Row) => srcCols.filter((c) => !c.secret).map((c) => value(r, c.name)).find((v) => v.includes('@')) ?? '';
	const targetEmail = (r: Row) => r.email.trim() || fallback(r);

	/** Once a mailbox exists, From and To stay as they are: the next rows go to the same place. */
	const locked = $derived(rows.some((r) => r.accountId != null && (!r.existing || r.status === 'added')));
	/** The destination cannot change once a mailbox of it is listed. */
	const lockedTo = $derived(locked || rows.some((r) => r.existing));
	/** Mailboxes already in the app on the destination server, not yet listed below. */
	const candidates = $derived(
		[...data.accounts.values()]
			.filter((a) => a.server_id === Number(serverId) && !rows.some((r) => r.accountId === a.id))
			.sort((a, b) => a.email.localeCompare(b.email))
	);
	const pending = $derived(rows.filter((r) => r.status !== 'added' && !blank(r)).length);
	const addedCount = $derived(rows.filter((r) => r.status === 'added').length);
	const failedCount = $derived(rows.filter((r) => r.status === 'failed').length);

	function goTenant() {
		if (source.type !== 'connection') return;
		const url = `/tenants/${source.connection.id}/migrate${Number(serverId) ? `?server=${Number(serverId)}` : ''}`;
		onclose();
		router.go(url);
	}

	function addExisting(list: Account[]) {
		const made = list.map((a) => {
			const row = newRow();
			row.accountId = a.id;
			row.existing = true;
			row.hasPassword = a.has_password;
			row.email = a.email;
			row.login = a.login ?? '';
			// The same login on the old server is the usual case: it starts from the address.
			if (source.type === 'server') row.src.login = a.email;
			return row;
		});
		rows = [...rows.filter((r) => r.accountId != null || !blank(r)), ...made];
	}

	function removeRow(row: Row) {
		rows = rows.filter((r) => r.key !== row.key);
		if (!rows.length) rows = [newRow()];
	}

	function fillFromList(list: ListRow[]) {
		const made = list.map(({ values }) => {
			const row = newRow();
			for (const c of srcCols) row.src[c.name] = values[`source_${c.name}`] ?? '';
			row.email = values.email ?? '';
			row.password = values.password ?? '';
			row.login = values.login ?? '';
			return row;
		});
		lastImport = { keys: new Set(made.map((r) => r.key)), destLogin };
		// A list that gives destination logins shows their column.
		if (made.some((r) => r.login.trim())) destLogin = true;
		rows = [...rows.filter((r) => r.accountId != null || !blank(r)), ...made];
	}

	function undoList() {
		if (!lastImport) return;
		const { keys, destLogin: before } = lastImport;
		rows = rows.filter((r) => !keys.has(r.key));
		if (!rows.length) rows = [newRow()];
		destLogin = before;
		lastImport = null;
	}

	async function applySource(row: Row, id: number): Promise<void> {
		if (source.type === 'none') return;
		let body: EndpointInput;
		if (source.type === 'server') {
			const s = source.server;
			body = { kind: 'imap', config: { host: s.imap_host, port: s.imap_port, security: s.imap_security, verify_tls: s.verify_tls, login: value(row, 'login') }, secrets: { password: row.src.password ?? '' } };
		} else {
			const config: Record<string, string> = {};
			const secrets: Record<string, string> = {};
			for (const c of srcCols) {
				const v = c.secret ? (row.src[c.name] ?? '') : value(row, c.name);
				if (!v) continue;
				if (c.secret) secrets[c.name] = v;
				else config[c.name] = v;
			}
			body = { kind: source.connection.kind, connection_id: source.connection.id, config, ...(Object.keys(secrets).length ? { secrets } : {}) };
		}
		data.upsertAccount(await put<Account>(`/accounts/${id}/source`, body));
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy || sameServer || onDirectory || source.type === 'none') return;
		const todo = rows.filter((r) => r.status !== 'added' && !blank(r));
		if (!todo.length) return;
		busy = true;
		const added: number[] = [];
		for (const row of todo) {
			row.status = 'busy';
			row.error = null;
			row.sourceFailed = false;
			try {
				let id = row.accountId;
				if (row.existing && row.password) data.upsertAccount(await put<Account>(`/accounts/${row.accountId}`, { password: row.password }));
				if (id == null) {
					const login = row.login.trim();
					const saved = await post<Account>('/accounts', { server_id: Number(serverId), email: targetEmail(row), password: row.password, ...(destLogin && login ? { login } : {}) });
					data.upsertAccount(saved);
					id = saved.id;
					row.accountId = id;
				}
				try {
					await applySource(row, id);
				} catch (err) {
					row.sourceFailed = true;
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
		if (added.length) data.test(added);
		if (rows.some((r) => r.status === 'failed')) return;
		const done = rows.filter((r) => r.status === 'added');
		// Several new mailboxes come back selected: the selection bar offers to test and start them.
		if (done.length > 1) for (const r of done) if (r.accountId != null) data.selected.add(r.accountId);
		toast(done.length === 1 ? m.migrations_account_saved() : m.migrations_new_added({ count: done.length }), 'signal');
		onclose();
		// One new migration goes on in its panel: that is where it starts.
		const only = done.length === 1 ? done[0].accountId : null;
		if (only != null) router.setParams({ account: String(only) });
	}
</script>

<Dialog title={m.migrations_add_label()} {onclose} wide>
	{#if !data.servers.length}
		<EmptyState icon={ServerIcon} title={m.migrations_new_no_server()} description={m.migrations_new_no_server_text()}>
			{#snippet action()}
				<Button variant="primary" icon={Plus} href="/servers" onclick={onclose} data-testid="new-add-server">{m.settings_servers_add()}</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<form id="new-migration-form" class="flex flex-col gap-6" onsubmit={submit}>
			<fieldset class="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
				<legend class="mb-2 p-0 text-sm font-semibold">{m.migrations_step_from()}</legend>
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" tabindex="-1" aria-label={m.migrations_step_from()} onkeydown={roving} data-testid="source-kinds">
					{#each fromTiles as tile, i (tile.value)}
						{@const on = fromValue === tile.value}
						<button type="button" role="radio" aria-checked={on} tabindex={tabStop(on, i, fromTiles.some((t) => t.value === fromValue))} class={tileClass(on)} disabled={locked || busy} onclick={() => (fromValue = tile.value)} data-kind={tile.value}>
							{#if on}<Check size={14} class="absolute top-1 end-1 text-ink" aria-hidden="true" />{/if}
							<KindIcon icon={tile.icon} size={28} />
							<span class="flex min-w-0 flex-col">
								<span class="truncate text-xs font-medium leading-tight">{tile.name}</span>
								{#if tile.sub}<span class={`truncate text-xs text-ink-3 ${tile.mono ? 'font-mono' : ''}`}>{tile.sub}</span>{/if}
							</span>
						</button>
					{/each}
				</div>
				<a class="inline-flex min-h-10 items-center gap-2 self-start text-ink-2 no-underline hover:text-ink" href="/servers" onclick={onclose}><Plus size={14} />{m.migrations_new_add_server()}</a>
			</fieldset>

			<fieldset class="relative m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
				<legend class="mb-2 flex items-center gap-2 p-0 text-sm font-semibold">{m.migrations_step_to()}<Help text={m.migrations_new_to_hint()} /></legend>
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" tabindex="-1" aria-label={m.migrations_account_server()} onkeydown={roving} data-testid="target-servers">
					{#each data.servers as item, i (item.id)}
						{@const on = Number(serverId) === item.id}
						<button type="button" role="radio" aria-checked={on} tabindex={tabStop(on, i, data.servers.some((s) => s.id === Number(serverId)))} class={tileClass(on)} disabled={lockedTo || busy} onclick={() => (serverId = item.id)}>
							{#if on}<Check size={14} class="absolute top-1 end-1 text-ink" aria-hidden="true" />{/if}
							<KindIcon icon={providerOf(item.imap_host).icon} mark={providerOf(item.imap_host).mark} size={28} />
							<span class="flex min-w-0 flex-col">
								<span class="truncate text-xs font-medium leading-tight">{item.name}</span>
								<span class="truncate font-mono text-xs text-ink-3">{prefs.host(item.imap_host)}</span>
							</span>
						</button>
					{/each}
				</div>
				{#if sameServer}
					<p class="m-0 text-xs text-warning-ink" role="alert" data-testid="same-server">{m.migrations_new_same_server()}</p>
				{/if}
			</fieldset>

			<fieldset class="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
				<legend class="mb-2 p-0 text-sm font-semibold">{m.migrations_new_mailboxes()}</legend>
				{#if onDirectory}
					<p class="m-0 text-ink-2">{m.sources_from_hint()}</p>
				{:else}
					<Toggle checked={destLogin} label={m.migrations_new_login_toggle()} onchange={(on) => (destLogin = on)} />

					<div class="flex flex-col" data-testid="new-rows" style:--src={`repeat(${Math.max(srcCols.length, 1)}, minmax(0, 1fr))`} style:--dst={`repeat(${destCount}, minmax(0, 1fr))`}>
						<div class="hidden gap-2 pb-1 text-xs font-semibold text-ink sm:flex" aria-hidden="true">
							<span class="min-w-0 flex-[2] truncate">{m.migrations_new_group_from({ name: sourceName })}</span>
							<span class="w-4 shrink-0"></span>
							<span class="min-w-0 flex-[3] truncate">{m.migrations_new_group_to({ name: targetName })}</span>
							<span class="w-10 shrink-0"></span>
						</div>
						<div class="hidden gap-2 pb-1 text-xs font-medium text-ink-2 sm:flex" aria-hidden="true">
							<div class="grid min-w-0 flex-[2] grid-cols-(--src) gap-2">
								{#each srcCols as col (col.name)}<span class="truncate">{col.label}</span>{/each}
							</div>
							<span class="w-4 shrink-0"></span>
							<div class="grid min-w-0 flex-[3] grid-cols-(--dst) gap-2">
								<span class="truncate">{m.migrations_new_dest_email()}</span>
								<span class="truncate">{m.migrations_new_dest_password()}</span>
								{#if destLogin}<span class="truncate">{m.migrations_new_dest_login()}</span>{/if}
							</div>
							<span class="w-10 shrink-0"></span>
						</div>

						{#each rows as row, i (row.key)}
							{@const done = row.status === 'added'}
							{@const live = !blank(row)}
							{@const destLocked = done || busy || row.accountId != null}
							<div class={`flex flex-col gap-1 py-2 ${i ? 'border-t border-line' : ''}`} data-testid="new-row" data-status={row.status}>
								<div class="flex flex-col gap-2 sm:flex-row sm:items-center">
									<div class="grid min-w-0 flex-[2] grid-cols-(--src) gap-2">
										{#each srcCols as col (col.name)}
											<input
												class={`input sm:placeholder:text-transparent ${col.secret ? '' : 'font-mono'}`}
												type={col.secret ? 'password' : 'text'}
												aria-label={col.label}
												placeholder={col.label}
												autocomplete={col.secret ? 'new-password' : 'off'}
												required={live}
												disabled={done || busy}
												bind:value={row.src[col.name]}
											/>
										{/each}
									</div>
									<ArrowRight size={16} class="shrink-0 self-center text-ink-3 max-sm:rotate-90" aria-hidden="true" />
									<div class="grid min-w-0 flex-[3] grid-cols-2 gap-2 sm:grid-cols-(--dst)">
										<input
											class={`input font-mono ${fallback(row) ? '' : 'sm:placeholder:text-transparent'} ${destLogin ? 'col-span-2 sm:col-span-1' : ''}`}
											type="email"
											aria-label={m.migrations_new_dest_email()}
											placeholder={fallback(row) || m.migrations_new_dest_email()}
											autocomplete="off"
											required={live && !fallback(row)}
											disabled={destLocked}
											bind:value={row.email}
										/>
										<input class="input sm:placeholder:text-transparent" type="password" aria-label={m.migrations_new_dest_password()} placeholder={row.existing && row.hasPassword ? m.migrations_new_dest_saved() : m.migrations_new_dest_password()} autocomplete="new-password" required={live && !row.existing} disabled={done || busy || (row.accountId != null && !row.existing) || (row.existing && row.hasPassword)} bind:value={row.password} />
										{#if destLogin}
											<input class="input font-mono sm:placeholder:text-transparent" aria-label={m.migrations_new_dest_login()} placeholder={m.migrations_new_dest_login()} autocomplete="off" disabled={destLocked} bind:value={row.login} />
										{/if}
									</div>
									<div class="flex w-10 shrink-0 self-end sm:self-center">
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
											<span class="min-w-0 text-xs break-words text-ink-2">{row.sourceFailed ? `${m.migrations_new_row_source_failed()} ${row.error ?? ''}` : row.error}</span>
										{/if}
									</div>
								{/if}
							</div>
						{/each}
					</div>

					<div class="flex flex-wrap items-center gap-2">
						<Button icon={Plus} onclick={() => (rows = [...rows, newRow()])} disabled={busy} data-testid="new-row-add">{m.migrations_add_account()}</Button>
					</div>

					<ListImport columns={listCols} shown={listShown} taken={listTaken} disabled={busy} undoable={listUndoable} onfill={fillFromList} onundo={undoList} />

					{#if candidates.length}
						<Disclosure title={m.migrations_new_existing({ count: candidates.length })} open data-testid="new-existing">
							<div class="flex flex-col gap-1">
								<p class="m-0 text-xs text-ink-2">{m.migrations_new_existing_hint({ server: targetName })}</p>
								<ul class="m-0 flex list-none flex-col p-0">
									{#each candidates as a (a.id)}
										<li class="flex min-h-10 items-center gap-2">
											<span class="min-w-0 flex-1 truncate font-mono text-xs">{prefs.email(a.email)}</span>
											<Button variant="ghost" icon={Plus} onclick={() => addExisting([a])} disabled={busy}>{m.migrations_new_existing_use()}</Button>
										</li>
									{/each}
								</ul>
								{#if candidates.length > 1}<div><Button onclick={() => addExisting(candidates)} disabled={busy} data-testid="new-existing-all">{m.migrations_new_existing_all()}</Button></div>{/if}
							</div>
						</Disclosure>
					{/if}

					{#if !busy && addedCount && failedCount}
						<Notice>{m.migrations_new_partial({ added: addedCount, failed: failedCount })}</Notice>
					{/if}
				{/if}
			</fieldset>
		</form>
	{/if}
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		{#if !data.servers.length}
			<!-- Nothing to add before a server exists. -->
		{:else if onDirectory}
			<Button variant="primary" onclick={goTenant} disabled={!Number(serverId)} data-testid="tenant-go">{m.sources_from_go()}</Button>
		{:else}
			<Button variant="primary" type="submit" form="new-migration-form" loading={busy} disabled={sameServer || source.type === 'none' || !pending || !Number(serverId)} data-testid="new-submit">
				{pending > 1 ? m.migrations_new_submit_many({ count: pending }) : m.migrations_new_submit_one()}
			</Button>
		{/if}
	{/snippet}
</Dialog>
