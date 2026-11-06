<script lang="ts">
	/**
	 * What belongs to one mailbox and not to a run of it: connection test, source, target and content.
	 * It lives in the panel so that the list stays calm. Reads only this account's entries; the
	 * nodes stay put through live events, so an open select stays open.
	 */
	import Calendar from '@lucide/svelte/icons/calendar';
	import Contact from '@lucide/svelte/icons/contact';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Mail from '@lucide/svelte/icons/mail';
	import Route from '@lucide/svelte/icons/route';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { currentResult } from '../../lib/logic/accounts.ts';
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
	}

	let { id, ondetails, onupload, onroute }: Props = $props();

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

<div class="flex flex-col gap-4">
	<div>
		<p class="m-0 mb-1 font-medium text-ink-2">{m.migrations_col_connection()}</p>
		<div class="flex flex-wrap items-center justify-between gap-2">
			<ConnectionCell {email} {phase} result={currentResult(account)} status={account.status} onclick={() => ondetails(id)} />
			<Button icon={FlaskConical} loading={!!phase} onclick={() => data.test([id])} data-testid="account-test">{phase ? m.migrations_test_running() : m.migrations_row_test()}</Button>
		</div>
	</div>

	<div>
		<p class="m-0 mb-1 font-medium text-ink-2">{m.migrations_col_pst()}</p>
		{#if sourceLabel}
			<button type="button" class="flex min-h-10 w-full min-w-0 flex-col justify-center rounded-btn text-left hover:bg-ghost" onclick={() => onroute(account)} aria-label={m.migrations_row_source_edit({ email })} data-testid="source-summary">
				<span class="truncate font-medium">{m.migrations_row_source({ label: sourceLabel })}</span>
				{#if sourceDetail}<span class="truncate font-mono text-xs text-ink-3">{sourceDetail}</span>{/if}
			</button>
		{:else}
			<select class="input" value={pstId == null ? '' : String(pstId)} aria-label={m.migrations_row_pst({ email })} onchange={onPstChange}>
				<option value="">{m.migrations_row_pst_none()}</option>
				{#each options as pst (pst.id)}
					<option value={String(pst.id)}>{prefs.file(pst.filename)} ({fmtBytes(pst.size)})</option>
				{/each}
				<option value={UPLOAD_VALUE}>{m.migrations_row_pst_upload()}</option>
			</select>
		{/if}
		{#if targetLabel}<p class="m-0 mt-1 truncate text-xs text-ink-2" data-testid="target-summary">{m.migrations_row_target({ label: targetLabel })}</p>{/if}
		{#if canRoute || sourceLabel || targetLabel}
			<div class="mt-1"><Button variant="ghost" icon={Route} onclick={() => onroute(account)} data-testid="account-route">{m.migrations_row_route()}</Button></div>
		{/if}
	</div>

	<div>
		<p class="m-0 mb-1 font-medium text-ink-2">{m.migrations_col_content()}</p>
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
					<Icon size={15} />{item.label}
				</button>
			{/each}
		</div>
	</div>

	{#if serverName || login}
		<div class="flex flex-col gap-1 text-xs text-ink-2">
			{#if serverName}<p class="m-0 truncate" title={serverName}>{m.migrations_row_server({ name: serverName })}</p>{/if}
			{#if login}<p class="m-0 truncate" title={login}>{m.migrations_row_login({ login })}</p>{/if}
		</div>
	{/if}
</div>
