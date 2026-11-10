<script lang="ts">
	/**
	 * A new migration: where the mail comes from (a file or a saved server), the server it goes to, then
	 * one row per mailbox, as many as needed. Servers are typed once on the Servers page: a row only gives
	 * what that mailbox adds (its logins and passwords). Rows are added one after the other; a row that
	 * was added is never sent again, and a row whose mailbox exists but whose source failed retries only
	 * the source. A directory (a Microsoft 365 tenant) lists its mailboxes itself, on its own screen.
	 */
	import ArrowRight from '@lucide/svelte/icons/arrow-right';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import FileUp from '@lucide/svelte/icons/file-up';
	import Hourglass from '@lucide/svelte/icons/hourglass';
	import Plus from '@lucide/svelte/icons/plus';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { Account, EndpointInput, Server, SourceConnection } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { isSecret, kindLabel } from '../../lib/logic/connector.ts';
	import { kindIcon, providerOf } from '../../lib/logic/providers.ts';
	import { FILE_ACCEPT } from '../../lib/logic/pst.ts';
	import { isDirectory, mailboxFields } from '../../lib/logic/serverKinds.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Help from '../../lib/ui/Help.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import Toggle from '../../lib/ui/Toggle.svelte';
	import { uploads } from '../../lib/upload/store.svelte.ts';

	interface Props {
		defaultServer?: number | null;
		onclose: () => void;
		oncsv?: () => void;
	}

	let { defaultServer = null, onclose, oncsv }: Props = $props();

	void sources.load();

	const FILE_TILE = '__files';
	const UPLOAD_VALUE = '__upload';
	const FILE_VALUE = '__file';

	type Source = { type: 'files' } | { type: 'server'; server: Server } | { type: 'connection'; connection: SourceConnection; directory: boolean };

	/** One column a row gives on the source side. */
	interface Col {
		name: string;
		label: string;
		secret: boolean;
	}

	interface Row {
		key: number;
		/** Source fields by name; a file source uses `file` and `upload` instead. */
		src: Record<string, string>;
		file: string;
		upload: File | null;
		email: string;
		password: string;
		login: string;
		/** Set once the mailbox exists: a retry then only sets the source. */
		accountId: number | null;
		status: 'idle' | 'busy' | 'added' | 'failed';
		error: string | null;
		sourceFailed: boolean;
	}

	let nextKey = 1;
	const newRow = (): Row => ({ key: nextKey++, src: {}, file: '', upload: null, email: '', password: '', login: '', accountId: null, status: 'idle', error: null, sourceFailed: false });

	// The form starts from the props once: later changes of the props do not rewrite what is typed.
	let serverId = $state(untrack(() => defaultServer ?? data.servers[0]?.id ?? 0));
	let fromValue = $state(FILE_TILE);
	let rows = $state<Row[]>([newRow()]);
	let destLogin = $state(false);
	let busy = $state(false);
	let pasteOpen = $state(false);
	let pasteText = $state('');
	let pasteError = $state<string | null>(null);

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
		return { type: 'files' };
	});
	const onDirectory = $derived(source.type === 'connection' && source.directory);
	const sameServer = $derived(source.type === 'server' && source.server.id === Number(serverId));

	const srcCols = $derived.by((): Col[] => {
		if (source.type === 'server') {
			return [
				{ name: 'login', label: m.migrations_account_login(), secret: false },
				{ name: 'password', label: m.migrations_account_password(), secret: true }
			];
		}
		if (source.type === 'connection' && !source.directory) {
			const role = connectors.list.find((c) => c.kind === source.connection.kind)?.source;
			return role ? mailboxFields(role, source.connection).map((f) => ({ name: f.name, label: f.label || f.name, secret: isSecret(f) })) : [];
		}
		return [];
	});
	const destCount = $derived(destLogin ? 3 : 2);
	/** The order of the pasted values: the columns as shown, the file excepted. */
	const pasteCols = $derived([
		...srcCols.map((c) => ({ label: c.label, set: (r: Row, v: string) => (r.src[c.name] = v) })),
		{ label: m.migrations_new_dest_email(), set: (r: Row, v: string) => (r.email = v) },
		{ label: m.migrations_new_dest_password(), set: (r: Row, v: string) => (r.password = v) },
		...(destLogin ? [{ label: m.migrations_new_dest_login(), set: (r: Row, v: string) => (r.login = v) }] : [])
	]);

	const files = $derived([...data.psts.values()].filter((p) => p.status === 'valid').sort((a, b) => a.filename.localeCompare(b.filename)));

	const fromTiles = $derived([
		{ value: FILE_TILE, name: m.migrations_new_source_files(), sub: '', mono: false, icon: kindIcon('pst') },
		...data.servers.map((s) => ({ value: `server-${s.id}`, name: s.name, sub: prefs.host(s.imap_host), mono: true, icon: providerOf(s.imap_host).icon })),
		...sources.list.map((c) => ({ value: `connection-${c.id}`, name: c.name, sub: kindLabel(connectors.list, c.kind), mono: false, icon: kindIcon(c.kind) }))
	]);

	const value = (r: Row, name: string) => (r.src[name] ?? '').trim();
	const blank = (r: Row) => r.file === '' && !r.email.trim() && !r.password && !r.login.trim() && srcCols.every((c) => !value(r, c.name));
	/** A source login that is an address stands for the destination address when that one is empty. */
	const fallback = (r: Row) => srcCols.filter((c) => !c.secret).map((c) => value(r, c.name)).find((v) => v.includes('@')) ?? '';
	const targetEmail = (r: Row) => r.email.trim() || fallback(r);

	/** Once a mailbox exists, From and To stay as they are: the next rows go to the same place. */
	const locked = $derived(rows.some((r) => r.accountId != null));
	const pending = $derived(rows.filter((r) => r.status !== 'added' && !blank(r)).length);
	const addedCount = $derived(rows.filter((r) => r.status === 'added').length);
	const failedCount = $derived(rows.filter((r) => r.status === 'failed').length);

	function goTenant() {
		if (source.type !== 'connection') return;
		const url = `/tenants/${source.connection.id}/migrate${Number(serverId) ? `?server=${Number(serverId)}` : ''}`;
		onclose();
		router.go(url);
	}

	function onFileChange(row: Row, e: Event & { currentTarget: HTMLSelectElement }) {
		const chosen = e.currentTarget.value;
		if (chosen !== UPLOAD_VALUE) {
			row.file = chosen;
			if (chosen !== FILE_VALUE) row.upload = null;
			return;
		}
		e.currentTarget.value = row.file;
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = FILE_ACCEPT;
		input.onchange = () => {
			const picked = input.files?.[0];
			if (!picked) return;
			row.upload = picked;
			row.file = FILE_VALUE;
		};
		input.click();
	}

	function removeRow(row: Row) {
		rows = rows.filter((r) => r.key !== row.key);
		if (!rows.length) rows = [newRow()];
	}

	function applyPaste() {
		pasteError = null;
		const lines = pasteText
			.split(/\r?\n/)
			.map((l) => l.trim())
			.filter(Boolean);
		if (lines.length && /email|login/i.test(lines[0]) && !lines[0].includes('@')) lines.shift();
		const parsed = lines.map((line) => {
			const values = line.split(/[;,\t]/).map((v) => v.trim());
			const row = newRow();
			pasteCols.forEach((c, i) => c.set(row, values[i] ?? ''));
			return row;
		});
		if (!parsed.length) {
			pasteError = m.migrations_new_paste_none();
			return;
		}
		rows = [...rows.filter((r) => r.accountId != null || !blank(r)), ...parsed];
		pasteText = '';
		pasteOpen = false;
	}

	async function applySource(row: Row, id: number): Promise<void> {
		if (source.type === 'files') {
			if (row.file === FILE_VALUE && row.upload) uploads.add([row.upload], 'size-asc', id);
			else if (row.file) await data.setPst(id, Number(row.file));
			return;
		}
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
		if (busy || sameServer || onDirectory) return;
		const todo = rows.filter((r) => r.status !== 'added' && !blank(r));
		if (!todo.length) return;
		busy = true;
		const added: number[] = [];
		let queued = false;
		for (const row of todo) {
			row.status = 'busy';
			row.error = null;
			row.sourceFailed = false;
			try {
				let id = row.accountId;
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
				if (row.file === FILE_VALUE && row.upload) queued = true;
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
		toast(queued ? m.migrations_row_upload_queued() : done.length === 1 ? m.migrations_account_saved() : m.migrations_new_added({ count: done.length }), 'signal');
		onclose();
		// One new migration goes on in its panel: that is where it starts.
		const only = done.length === 1 ? done[0].accountId : null;
		if (only != null) router.setParams({ account: String(only) });
	}

	const tileClass = (selected: boolean) =>
		`flex min-h-12 min-w-0 items-center gap-2 rounded-btn border px-2 text-left transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-50 ${selected ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`;
</script>

<Dialog title={m.migrations_add_label()} {onclose} wide>
	<form id="new-migration-form" class="flex flex-col gap-6" onsubmit={submit}>
		<fieldset class="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
			<legend class="mb-2 p-0 text-sm font-semibold">{m.migrations_step_from()}</legend>
			<div class="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" aria-label={m.migrations_step_from()} data-testid="source-kinds">
				{#each fromTiles as tile (tile.value)}
					<button type="button" role="radio" aria-checked={fromValue === tile.value} class={tileClass(fromValue === tile.value)} disabled={locked || busy} onclick={() => (fromValue = tile.value)} data-kind={tile.value}>
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
			<div class="grid grid-cols-2 gap-2 sm:grid-cols-3" role="radiogroup" aria-label={m.migrations_account_server()} data-testid="target-servers">
				{#each data.servers as item (item.id)}
					<button type="button" role="radio" aria-checked={Number(serverId) === item.id} class={tileClass(Number(serverId) === item.id)} disabled={locked || busy} onclick={() => (serverId = item.id)}>
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
					<div class="hidden gap-2 pb-1 text-xs font-medium text-ink-2 sm:flex" aria-hidden="true">
						<div class="grid min-w-0 flex-[2] grid-cols-(--src) gap-2">
							{#if source.type === 'files'}<span>{m.migrations_new_col_file()}</span>{/if}
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
									{#if source.type === 'files'}
										<select class="input" aria-label={m.migrations_new_col_file()} value={row.file} onchange={(e) => onFileChange(row, e)} disabled={done || busy} data-testid="new-source">
											<option value="">{m.migrations_new_source_later()}</option>
											{#each files as pst (pst.id)}
												<option value={String(pst.id)}>{prefs.file(pst.filename)} ({fmtBytes(pst.size)})</option>
											{/each}
											{#if row.upload}<option value={FILE_VALUE}>{m.migrations_new_source_file({ name: prefs.file(row.upload.name) })}</option>{/if}
											<option value={UPLOAD_VALUE}>{m.migrations_row_pst_upload()}</option>
										</select>
									{/if}
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
									<input class="input sm:placeholder:text-transparent" type="password" aria-label={m.migrations_new_dest_password()} placeholder={m.migrations_new_dest_password()} autocomplete="new-password" required={live} disabled={destLocked} bind:value={row.password} />
									{#if destLogin}
										<input class="input font-mono sm:placeholder:text-transparent" aria-label={m.migrations_new_dest_login()} placeholder={m.migrations_new_dest_login()} autocomplete="off" disabled={destLocked} bind:value={row.login} />
									{/if}
								</div>
								<div class="flex w-10 shrink-0 self-end sm:self-center">
									{#if !done && row.accountId == null}
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

				<Disclosure title={m.migrations_new_paste()} bind:open={pasteOpen} data-testid="new-paste">
					<div class="flex flex-col gap-2">
						<p class="m-0 text-xs text-ink-2">{m.migrations_new_paste_hint()}</p>
						<code class="block rounded-plate bg-surface-2 p-2 font-mono text-xs break-words">{pasteCols.map((c) => c.label).join('; ')}</code>
						<textarea class="input min-h-24 font-mono text-xs" aria-label={m.migrations_new_paste()} bind:value={pasteText} spellcheck="false" autocomplete="off"></textarea>
						{#if pasteError}<p class="m-0 text-xs text-warning-ink" role="alert">{pasteError}</p>{/if}
						<div><Button onclick={applyPaste} disabled={!pasteText.trim() || busy}>{m.migrations_new_paste_apply()}</Button></div>
					</div>
				</Disclosure>

				{#if !busy && addedCount && failedCount}
					<Notice>{m.migrations_new_partial({ added: addedCount, failed: failedCount })}</Notice>
				{/if}
			{/if}
		</fieldset>
	</form>
	{#snippet actions()}
		{#if oncsv}
			<span class="mr-auto"><Button variant="ghost" icon={FileUp} onclick={oncsv} disabled={busy} data-testid="new-csv">{m.migrations_add_csv()}</Button></span>
		{/if}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		{#if onDirectory}
			<Button variant="primary" onclick={goTenant} disabled={!Number(serverId)} data-testid="tenant-go">{m.sources_from_go()}</Button>
		{:else}
			<Button variant="primary" type="submit" form="new-migration-form" loading={busy} disabled={sameServer || !pending || !Number(serverId)} data-testid="new-submit">
				{pending > 1 ? m.migrations_new_submit_many({ count: pending }) : m.migrations_new_submit_one()}
			</Button>
		{/if}
	{/snippet}
</Dialog>
