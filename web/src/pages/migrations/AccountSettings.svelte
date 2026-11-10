<script lang="ts">
	/**
	 * One mailbox read as a sentence: where it comes from, where it goes, what is copied, then one button
	 * to start. Each side says whether it is ready. It lives in the panel so that the list stays calm.
	 * Reads only this mailbox's entries; the nodes stay put through live events, so an open select stays open.
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
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { currentResult, importPlan, isActive, testPassed } from '../../lib/logic/accounts.ts';
	import { endpointSummary } from '../../lib/logic/connector.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ConnectionCell from './ConnectionCell.svelte';

	const UPLOAD_VALUE = '__upload';

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

	const job = $derived(data.jobs.get(id));
	const running = $derived(isActive(job));
	const plan = $derived(importPlan([account], (accountId) => data.jobs.get(accountId), (fileId) => data.psts.get(fileId)));
	const sourceReady = $derived(!plan.noPst.length);
	const result = $derived(currentResult(account));
	const loginRefused = $derived(account.status === 'auth_failed' || account.status === 'error' || (!!result && !testPassed(result)));
	const loginOk = $derived(!loginRefused && (result ? testPassed(result) : account.status === 'ok'));
	const blocked = $derived(plan.noPst.length ? m.migrations_start_need_source() : plan.noPassword.length ? m.migrations_start_need_password() : '');

	const CONTENT = $derived([
		{ key: 'mail' as const, on: account.options.mail, icon: Mail, label: m.migrations_row_content_mail() },
		{ key: 'calendar' as const, on: account.options.calendar, icon: Calendar, label: m.migrations_row_content_calendar() },
		{ key: 'contacts' as const, on: account.options.contacts, icon: Contact, label: m.migrations_row_content_contacts() }
	]);

	function onPstChange(e: Event & { currentTarget: HTMLSelectElement }) {
		const value = e.currentTarget.value;
		if (value === UPLOAD_VALUE) {
			e.currentTarget.value = String(pstId ?? '');
			onupload(id);
			return;
		}
		void data.setPst(id, value ? Number(value) : null);
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
			{@render mark(sourceReady, false)}{m.migrations_step_from()}
		</h3>
		{#if sourceLabel}
			<button type="button" class="flex min-h-10 w-full min-w-0 flex-col justify-center rounded-btn text-left hover:bg-ghost" onclick={() => onroute(account)} aria-label={m.migrations_row_source_edit({ email })} data-testid="source-summary">
				<span class="truncate font-medium">{m.migrations_row_source({ label: sourceLabel })}</span>
				{#if sourceDetail}<span class="truncate font-mono text-xs text-ink-3">{sourceDetail}</span>{/if}
			</button>
		{:else}
			<select class="input" value={pstId == null ? '' : String(pstId)} aria-label={m.migrations_row_pst({ email })} onchange={onPstChange}>
				<option value="">{m.migrations_step_from_choose()}</option>
				{#each options as pst (pst.id)}
					<option value={String(pst.id)}>{prefs.file(pst.filename)} ({fmtBytes(pst.size)})</option>
				{/each}
				<option value={UPLOAD_VALUE}>{m.migrations_row_pst_upload()}</option>
			</select>
		{/if}
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
			<ConnectionCell {email} {phase} {result} status={account.status} onclick={() => ondetails(id)} />
			<span class="flex-1"></span>
			<Button icon={FlaskConical} loading={!!phase} onclick={() => data.test([id])} data-testid="account-test">{phase ? m.migrations_test_running() : m.migrations_row_test()}</Button>
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
