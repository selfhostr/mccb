<script lang="ts">
	/**
	 * One mailbox read as a sentence: where it comes from, where it goes, what is copied, then one button
	 * to start. Each side says whether it is ready. It lives in the panel so that the list stays calm.
	 * Reads only this mailbox's entries; the nodes stay put through live events, so an open select stays open.
	 * The source is picked here: a PST file, a saved server (with this mailbox's login and password), or any
	 * other connector through the route dialog.
	 */
	import Calendar from '@lucide/svelte/icons/calendar';
	import Circle from '@lucide/svelte/icons/circle';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import Contact from '@lucide/svelte/icons/contact';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import KeyRound from '@lucide/svelte/icons/key-round';
	import Mail from '@lucide/svelte/icons/mail';
	import Play from '@lucide/svelte/icons/play';
	import Route from '@lucide/svelte/icons/route';
	import Save from '@lucide/svelte/icons/save';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, put } from '../../lib/api/client.ts';
	import type { Account, EndpointInput, Server } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { currentResult, importPlan, isActive, testPassed } from '../../lib/logic/accounts.ts';
	import { endpointSummary, hasCustomSource, PST_KIND } from '../../lib/logic/connector.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import NoDavNotice from './NoDavNotice.svelte';
	import ConnectionCell, { sourceProbes, sourceTestable, testSource } from './ConnectionCell.svelte';

	const UPLOAD_VALUE = '__upload';
	const PST_TYPE = 'pst';
	const ADVANCED_TYPE = 'advanced';
	const serverType = (s: Pick<Server, 'id'>) => `server-${s.id}`;

	interface Props {
		id: number;
		ondetails: (id: number) => void;
		onupload: (id: number) => void;
		onroute: (account: Account) => void;
		onedit: (account: Account) => void;
		onstart: (account: Account) => void;
	}

	let { id, ondetails, onupload, onroute, onedit, onstart }: Props = $props();

	let last: Account | undefined;
	const account = $derived.by(() => (last = data.accounts.get(id) ?? last) as Account);
	const email = $derived(prefs.email(account.email));
	const login = $derived(account.login && account.login !== account.email ? prefs.email(account.login) : '');
	const pstId = $derived(account.pst_id);
	const serverName = $derived(data.servers.find((s) => s.id === account.server_id)?.name ?? '');
	const phase = $derived(data.testing.get(id));
	// Another source than the PST, or an own target: said in words, edited in the route dialog.
	const customSource = $derived(endpointSummary(connectors.list, account.source, 'source'));
	const sourceLabel = $derived(customSource ? customSource.label : '');
	const sourceDetail = $derived(customSource ? prefs.text(customSource.detail) : '');
	const targetLabel = $derived(endpointSummary(connectors.list, account.target, 'target')?.label ?? '');
	const canRoute = $derived(connectors.list.some((c) => c.source && c.kind !== 'pst') || connectors.list.some((c) => c.target));
	const options = $derived([...data.psts.values()].filter((p) => p.status === 'valid' || p.id === pstId).sort((a, b) => a.filename.localeCompare(b.filename)));

	// The source type: a saved server is an IMAP source with that server's settings. The destination's own server is not offered.
	const sourceServers = $derived(data.servers.filter((s) => s.id !== account.server_id));
	const storedServer = $derived.by(() => {
		const src = account.source;
		if (src?.kind !== 'imap' || src.connection) return undefined;
		return data.servers.find((s) => s.imap_host === src.config?.host && s.imap_port === Number(src.config?.port ?? s.imap_port));
	});
	const storedType = $derived(storedServer ? serverType(storedServer) : hasCustomSource(account) ? ADVANCED_TYPE : PST_TYPE);
	/** A type picked but not saved yet: the PST select or the server's fields show until a value is saved. */
	let picked = $state<string | null>(null);
	const sourceType = $derived(picked ?? storedType);
	const pickedServer = $derived(data.servers.find((s) => serverType(s) === sourceType));
	let srcLogin = $state(untrack(() => initialLogin()));
	let srcPassword = $state('');
	let srcBusy = $state(false);
	let srcError = $state<string | null>(null);
	const keepsPassword = $derived(!!pickedServer && pickedServer.id === storedServer?.id && !!account.source?.has_secret);
	const sourceProbe = $derived(sourceTestable(account) ? sourceProbes.get(id) : undefined);

	function initialLogin(): string {
		const stored = account.source?.kind === 'imap' ? account.source.config?.login : undefined;
		return typeof stored === 'string' && stored ? stored : account.email;
	}

	const job = $derived(data.jobs.get(id));
	const running = $derived(isActive(job));
	const plan = $derived(importPlan([account], (accountId) => data.jobs.get(accountId), (fileId) => data.psts.get(fileId), data.davEnabled));
	const noDavServer = $derived(plan.noDav.length ? data.servers.find((s) => s.id === account.server_id) : undefined);
	const sourceReady = $derived(!plan.noPst.length && picked == null);
	const sourceRefused = $derived(sourceProbe?.state === 'done' && !sourceProbe.ok);
	const result = $derived(currentResult(account));
	const loginRefused = $derived(account.status === 'auth_failed' || account.status === 'error' || (!!result && !testPassed(result)));
	const loginOk = $derived(!loginRefused && (result ? testPassed(result) : account.status === 'ok'));
	const blocked = $derived(plan.noPst.length ? m.migrations_start_need_source() : plan.noPassword.length ? m.migrations_start_need_password() : '');

	const CONTENT = $derived([
		{ key: 'mail' as const, on: account.options.mail, icon: Mail, label: m.migrations_row_content_mail() },
		{ key: 'calendar' as const, on: account.options.calendar, icon: Calendar, label: m.migrations_row_content_calendar() },
		{ key: 'contacts' as const, on: account.options.contacts, icon: Contact, label: m.migrations_row_content_contacts() }
	]);

	function onTypeChange(e: Event & { currentTarget: HTMLSelectElement }) {
		const value = e.currentTarget.value;
		srcError = null;
		if (value === ADVANCED_TYPE && storedType !== ADVANCED_TYPE) {
			// Any other connector is set in the route dialog; the select stays on what is chosen.
			e.currentTarget.value = sourceType;
			onroute(account);
			return;
		}
		picked = value === storedType ? null : value;
		srcPassword = '';
		srcLogin = initialLogin();
	}

	async function putSource(body: EndpointInput): Promise<boolean> {
		srcBusy = true;
		srcError = null;
		try {
			data.upsertAccount(await put<Account>(`/accounts/${id}/source`, body));
			picked = null;
			return true;
		} catch (error) {
			srcError = errorText(error);
			return false;
		} finally {
			srcBusy = false;
		}
	}

	async function onPstChange(e: Event & { currentTarget: HTMLSelectElement }) {
		const value = e.currentTarget.value;
		// Leaving a connector source: the source goes back to the PST first, else the file would not be read.
		const leaving = hasCustomSource(account);
		if (value === UPLOAD_VALUE) {
			e.currentTarget.value = leaving || pstId == null ? '' : String(pstId);
			if (leaving && !(await putSource({ kind: PST_KIND }))) return;
			onupload(id);
			return;
		}
		if (leaving) {
			if (await putSource({ kind: PST_KIND, config: value ? { pst_id: Number(value) } : {} })) toast(m.migrations_source_saved(), 'signal');
			return;
		}
		void data.setPst(id, value ? Number(value) : null);
	}

	async function saveServer(e: SubmitEvent) {
		e.preventDefault();
		const server = pickedServer;
		if (!server || srcBusy) return;
		if (!srcPassword && !keepsPassword) {
			srcError = m.migrations_source_password_needed();
			return;
		}
		const body: EndpointInput = {
			kind: 'imap',
			config: { host: server.imap_host, port: server.imap_port, security: server.imap_security, verify_tls: server.verify_tls, login: srcLogin.trim() || account.email },
			...(srcPassword ? { secrets: { password: srcPassword } } : {})
		};
		if (!(await putSource(body))) return;
		srcPassword = '';
		toast(m.migrations_source_saved(), 'signal');
		// The new settings are tried at once: the mark of "Depuis" says whether the old server accepts them.
		const saved = data.accounts.get(id);
		if (saved) void testSource(saved);
	}

	/** One button tries both sides: the destination through the server's stored test, the source when it is a connector. */
	function runTest() {
		data.test([id]);
		if (sourceTestable(account)) void testSource(account);
	}
</script>

{#snippet mark(ready: boolean, problem: boolean)}
	{#if problem}
		<span class="text-warning-ink"><TriangleAlert size={16} aria-hidden="true" /></span>
		<span class="sr-only">{m.migrations_step_problem()}</span>
	{:else if ready}
		<span class="text-signal-ink"><CircleCheck size={16} aria-hidden="true" /></span>
		<span class="sr-only">{m.migrations_step_ready()}</span>
	{:else}
		<span class="text-ink-3"><Circle size={16} aria-hidden="true" /></span>
		<span class="sr-only">{m.migrations_step_todo()}</span>
	{/if}
{/snippet}

<div class="flex flex-col gap-5">
	<section class="flex flex-col gap-2" aria-labelledby={`from-${id}`} data-testid="step-from">
		<h3 id={`from-${id}`} class="m-0 flex items-center gap-2 text-sm font-semibold">
			{@render mark(sourceReady, sourceRefused)}{m.migrations_step_from()}
		</h3>
		<select class="input" value={sourceType} aria-label={m.migrations_source_type({ email })} onchange={onTypeChange} disabled={srcBusy} data-testid="source-type">
			<option value={PST_TYPE}>{m.migrations_route_source_pst()}</option>
			{#each sourceServers as server (server.id)}
				<option value={serverType(server)}>{m.migrations_source_server({ name: server.name })}</option>
			{/each}
			{#if storedServer && storedServer.id === account.server_id}<option value={serverType(storedServer)}>{m.migrations_source_server({ name: storedServer.name })}</option>{/if}
			<option value={ADVANCED_TYPE}>{m.migrations_source_advanced()}</option>
		</select>
		{#if sourceType === PST_TYPE}
			<select class="input" value={hasCustomSource(account) || pstId == null ? '' : String(pstId)} aria-label={m.migrations_row_pst({ email })} onchange={onPstChange} disabled={srcBusy}>
				<option value="">{m.migrations_step_from_choose()}</option>
				{#each options as pst (pst.id)}
					<option value={String(pst.id)}>{prefs.file(pst.filename)} ({fmtBytes(pst.size)})</option>
				{/each}
				<option value={UPLOAD_VALUE}>{m.migrations_row_pst_upload()}</option>
			</select>
		{:else if pickedServer}
			<form class="flex flex-col gap-2" onsubmit={saveServer} data-testid="source-server">
				<Field label={m.migrations_new_src_login()}>
					<input class="input" autocomplete="off" bind:value={srcLogin} oninput={() => (srcError = null)} data-testid="source-login" />
				</Field>
				<Field label={m.migrations_new_src_password()} hint={keepsPassword ? m.migrations_source_password_kept() : undefined}>
					<input class="input" type="password" autocomplete="new-password" bind:value={srcPassword} oninput={() => (srcError = null)} data-testid="source-password" />
				</Field>
				<div><Button type="submit" icon={Save} loading={srcBusy} data-testid="source-save">{m.migrations_source_save()}</Button></div>
			</form>
		{:else if sourceLabel}
			<button type="button" class="flex min-h-10 w-full min-w-0 flex-col justify-center rounded-btn text-left hover:bg-ghost" onclick={() => onroute(account)} aria-label={m.migrations_row_source_edit({ email })} data-testid="source-summary">
				<span class="truncate font-medium">{m.migrations_row_source({ label: sourceLabel })}</span>
				{#if sourceDetail}<span class="truncate font-mono text-xs text-ink-3">{sourceDetail}</span>{/if}
			</button>
		{/if}
		{#if srcError}<p class="m-0 text-xs text-warning-ink" role="alert" data-testid="source-error">{srcError}</p>{/if}
		{#if canRoute || sourceLabel || targetLabel}
			<div><Button variant="ghost" icon={Route} onclick={() => onroute(account)} data-testid="account-route">{m.migrations_row_route()}</Button></div>
		{/if}
	</section>

	<section class="flex flex-col gap-2" aria-labelledby={`to-${id}`} data-testid="step-to">
		<h3 id={`to-${id}`} class="m-0 flex items-center gap-2 text-sm font-semibold">
			{@render mark(loginOk, loginRefused)}{m.migrations_step_to()}
		</h3>
		<p class="m-0 min-w-0 truncate" title={account.email}>
			{targetLabel ? m.migrations_step_to_own({ email, target: targetLabel }) : m.migrations_step_to_line({ email, server: serverName })}
		</p>
		{#if targetLabel}<p class="m-0 truncate text-xs text-ink-2" data-testid="target-summary">{m.migrations_row_target({ label: targetLabel })}</p>{/if}
		{#if login}<p class="m-0 truncate text-xs text-ink-2" title={login}>{m.migrations_row_login({ login })}</p>{/if}
		<div class="flex flex-wrap items-center gap-2">
			<ConnectionCell {email} {phase} {result} status={account.status} source={sourceProbe} onclick={() => ondetails(id)} />
			<span class="flex-1"></span>
			<Button icon={FlaskConical} loading={!!phase} onclick={runTest} data-testid="account-test">{phase ? m.migrations_test_running() : m.migrations_row_test()}</Button>
		</div>
		{#if loginRefused && !phase}<p class="m-0 text-xs text-warning-ink" data-testid="login-refused">{m.migrations_step_to_refused()}</p>{/if}
		<div><Button variant="ghost" icon={KeyRound} onclick={() => onedit(account)} data-testid="account-edit">{m.migrations_row_edit()}</Button></div>
	</section>

	<section class="flex flex-col gap-2" aria-labelledby={`what-${id}`}>
		<h3 id={`what-${id}`} class="m-0 text-sm font-semibold">{m.migrations_step_what()}</h3>
		<div class="flex flex-wrap gap-1.5">
			{#each CONTENT as item (item.key)}
				{@const Icon = item.icon}
				<button
					type="button"
					class={`inline-flex min-h-10 items-center gap-2 rounded-btn border px-3 font-medium transition-colors duration-200 ${item.on ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
					aria-pressed={item.on}
					aria-label={m.migrations_row_content_toggle({ email, content: item.label })}
					onclick={() => data.setOption(id, item.key, !item.on)}
				>
					<Icon size={16} />{item.label}
				</button>
			{/each}
		</div>
		{#if noDavServer}<NoDavNotice server={noDavServer} />{/if}
	</section>

	{#if !running}
		<div class="flex flex-col gap-2 border-t border-line pt-4" data-testid="step-start">
			<div>
				<Button variant="primary" icon={Play} disabled={!!blocked} onclick={() => onstart(account)} data-testid="account-start">
					{job ? m.migrations_start_again() : m.migrations_start()}
				</Button>
			</div>
			<p class="m-0 text-xs text-ink-2" data-testid="start-hint">{blocked || (loginOk ? m.migrations_start_ready() : m.migrations_start_unchecked())}</p>
		</div>
	{/if}
</div>
