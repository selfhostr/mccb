<script lang="ts">
	/**
	 * A new migration: where the mail comes from and the mailbox it goes to, in one form. Saving opens the
	 * mailbox's panel, where the start button is. Editing changes the password and the login only.
	 * The password is never sent back: empty keeps it. The login is sent only when it changed: for the
	 * server a new login is a new target, and everything is imported again.
	 */
	import FileUp from '@lucide/svelte/icons/file-up';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { Account } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { FILE_ACCEPT } from '../../lib/logic/pst.ts';
	import { kindsFor } from '../../lib/logic/connector.ts';
	import { kindIcon, providerOf } from '../../lib/logic/providers.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import EndpointEditor from './EndpointEditor.svelte';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import PolicyFields from '../settings/PolicyFields.svelte';

	const UPLOAD_VALUE = '__upload';
	const FILE_VALUE = '__file';

	interface Props {
		account?: Account | null;
		defaultServer?: number | null;
		onclose: () => void;
		/** Only when adding: the dialog offers the CSV import instead. */
		oncsv?: () => void;
	}

	let { account = null, defaultServer = null, onclose, oncsv }: Props = $props();

	// The form starts from the props once: later changes of the props do not rewrite what is typed.
	const initialServer = untrack(() => defaultServer ?? data.servers[0]?.id ?? 0);
	let serverId = $state(initialServer);
	let email = $state('');
	let password = $state('');
	const initialLogin = untrack(() => (account?.login && account.login !== account.email ? account.login : ''));
	let login = $state(initialLogin);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let policyError = $state<string | null>(null);
	let policy = $state<PolicyFields>();

	// The source of a new migration: a file already on the server, or one to send once the mailbox exists.
	let source = $state('');
	let file = $state.raw<File | null>(null);
	const files = $derived([...data.psts.values()].filter((p) => p.status === 'valid').sort((a, b) => a.filename.localeCompare(b.filename)));
	const serverName = $derived(data.servers.find((s) => s.id === Number(serverId))?.name ?? '');

	// Where the mail comes from: an uploaded file, or a server read through a connector.
	const FILE_TILE = '__files';
	let sourceKind = $state(FILE_TILE);
	let editor = $state<EndpointEditor>();
	const sourceKinds = $derived(kindsFor(connectors.list, 'source'));
	// The editor needs a mailbox; before it exists it gets an empty one, and the test runs without it.
	const draft = { id: 0, source: null, target: null } as unknown as Account;

	function onSourceChange(e: Event & { currentTarget: HTMLSelectElement }) {
		const value = e.currentTarget.value;
		if (value !== UPLOAD_VALUE) {
			source = value;
			if (value !== FILE_VALUE) file = null;
			return;
		}
		e.currentTarget.value = source;
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = FILE_ACCEPT;
		input.onchange = () => {
			const chosen = input.files?.[0];
			if (!chosen) return;
			file = chosen;
			source = FILE_VALUE;
		};
		input.click();
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		policyError = null;
		try {
			// The rules first for an existing mailbox: a refusal leaves everything as it was.
			if (account && policy?.dirty()) {
				try {
					await put(`/accounts/${account.id}/policy`, policy.body());
				} catch (err) {
					policyError = errorText(err);
					return;
				}
			}
			const sourceBody = !account && sourceKind !== FILE_TILE ? editor?.body() : undefined;
			if (sourceBody && 'problem' in sourceBody) return;
			const saved = account
				? await put<Account>(`/accounts/${account.id}`, { ...(password ? { password } : {}), ...(login !== initialLogin ? { login } : {}) })
				: await post<Account>('/accounts', { server_id: Number(serverId), email: email.trim(), password, ...(login ? { login } : {}) });
			data.upsertAccount(saved);
			if (!account) {
				if (sourceBody && 'input' in sourceBody) data.upsertAccount(await put<Account>(`/accounts/${saved.id}/source`, sourceBody.input));
				else if (file && source === FILE_VALUE) uploads.add([file], 'size-asc', saved.id);
				else if (source) await data.setPst(saved.id, Number(source));
			}
			toast(file && source === FILE_VALUE && !account ? m.migrations_row_upload_queued() : m.migrations_account_saved(), 'signal');
			if (password) data.test([saved.id]);
			onclose();
			// A new migration goes on in its panel: that is where it starts.
			if (!account) router.setParams({ account: String(saved.id) });
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={account ? m.migrations_account_edit_title({ email: prefs.email(account.email) }) : m.migrations_add_label()} {onclose} wide={!account}>
	<form id="account-form" class="flex flex-col gap-5" onsubmit={submit}>
		{#if !account}
			<fieldset class="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
				<legend class="mb-2 p-0 text-sm font-semibold">{m.migrations_step_from()}</legend>
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-4" role="radiogroup" aria-label={m.migrations_step_from()} data-testid="source-kinds">
					{#each [{ kind: FILE_TILE, label: m.migrations_new_source_files() }, ...sourceKinds.map((c) => ({ kind: c.kind, label: c.label || c.kind }))] as tile (tile.kind)}
						<button
							type="button"
							role="radio"
							aria-checked={sourceKind === tile.kind}
							class={`flex min-h-12 items-center gap-2 rounded-btn border px-2 text-left font-medium transition-colors duration-200 ${sourceKind === tile.kind ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
							onclick={() => (sourceKind = tile.kind)}
							data-kind={tile.kind}
						>
							<KindIcon icon={kindIcon(tile.kind === FILE_TILE ? 'pst' : tile.kind)} size={28} />
							<span class="min-w-0 text-xs leading-tight">{tile.label}</span>
						</button>
					{/each}
				</div>
				{#if sourceKind === FILE_TILE}
					<Field label={m.migrations_new_source()} hint={m.migrations_new_source_hint()}>
						<select class="input" value={source} onchange={onSourceChange} data-testid="new-source">
							<option value="">{m.migrations_new_source_later()}</option>
							{#each files as pst (pst.id)}
								<option value={String(pst.id)}>{prefs.file(pst.filename)} ({fmtBytes(pst.size)})</option>
							{/each}
							{#if file}<option value={FILE_VALUE}>{m.migrations_new_source_file({ name: prefs.file(file.name) })}</option>{/if}
							<option value={UPLOAD_VALUE}>{m.migrations_row_pst_upload()}</option>
						</select>
					</Field>
				{:else}
					{#key sourceKind}
						<div class="rounded-card border border-line p-3"><EndpointEditor bind:this={editor} role="source" account={draft} fixedKind={sourceKind} /></div>
					{/key}
				{/if}
			</fieldset>
			<fieldset class="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
				<legend class="mb-1 p-0 text-sm font-semibold">{m.migrations_step_to()}</legend>
				<div class="flex flex-col gap-1">
					<span class="font-medium text-ink-2">{m.migrations_account_server()}</span>
					<div class="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label={m.migrations_account_server()} data-testid="target-servers">
						{#each data.servers as item (item.id)}
							{@const brand = providerOf(item.imap_host)}
							<button
								type="button"
								role="radio"
								aria-checked={Number(serverId) === item.id}
								class={`flex min-h-12 items-center gap-2 rounded-btn border px-2 text-left transition-colors duration-200 ${Number(serverId) === item.id ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
								onclick={() => (serverId = item.id)}
							>
								<KindIcon icon={brand.icon} size={28} />
								<span class="flex min-w-0 flex-col"><span class="truncate font-medium">{item.name}</span><span class="truncate font-mono text-xs text-ink-3">{prefs.host(item.imap_host)}</span></span>
							</button>
						{/each}
					</div>
					<a class="self-start text-xs" href="/servers" onclick={onclose}>{m.migrations_new_add_server()}</a>
				</div>
				<Field label={m.migrations_account_email()} hint={serverName ? m.migrations_new_email_hint({ server: serverName }) : undefined}>
					<input class="input font-mono" type="email" required autocomplete="off" bind:value={email} />
				</Field>
				<Field label={m.migrations_account_password()} hint={m.migrations_new_password_hint()}>
					<input class="input" type="password" required autocomplete="new-password" bind:value={password} />
				</Field>
				<Field label={m.migrations_account_login()} hint={m.migrations_account_login_hint()}>
					<input class="input font-mono" autocomplete="off" bind:value={login} />
				</Field>
			</fieldset>
		{:else}
			<Field label={m.migrations_account_password()} hint={m.migrations_account_password_keep()}>
				<input class="input" type="password" autocomplete="new-password" bind:value={password} />
			</Field>
			<Field label={m.migrations_account_login()} hint={m.migrations_account_login_hint()}>
				<input class="input font-mono" autocomplete="off" bind:value={login} />
			</Field>
			<PolicyFields bind:this={policy} url={`/accounts/${account.id}/policy`} server={false} />
		{/if}
		{#if policyError}<ErrorNotice title={m.policy_save_failed()} message={policyError} />{/if}
		{#if error}<ErrorNotice title={m.migrations_account_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		{#if !account && oncsv}
			<span class="mr-auto"><Button variant="ghost" icon={FileUp} onclick={oncsv} data-testid="new-csv">{m.migrations_add_csv()}</Button></span>
		{/if}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="account-form" loading={busy}>{account ? m.common_save() : m.migrations_add_account()}</Button>
	{/snippet}
</Dialog>
