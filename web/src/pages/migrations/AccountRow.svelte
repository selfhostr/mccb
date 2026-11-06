<script lang="ts">
	/**
	 * One row. It receives an id and reads only its own map entries, and every value used by
	 * the markup is a $derived primitive: a live event for another account runs nothing here,
	 * and an event for this one touches only the text that changed. An open select stays open.
	 */
	import Calendar from '@lucide/svelte/icons/calendar';
	import Contact from '@lucide/svelte/icons/contact';
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import ArrowUp from '@lucide/svelte/icons/arrow-up';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import KeyRound from '@lucide/svelte/icons/key-round';
	import Mail from '@lucide/svelte/icons/mail';
	import Route from '@lucide/svelte/icons/route';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account, Pst } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { currentResult } from '../../lib/logic/accounts.ts';
	import { endpointSummary } from '../../lib/logic/connector.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ConnectionCell from './ConnectionCell.svelte';
	import ProgressCell from './ProgressCell.svelte';

	const UPLOAD_VALUE = '__upload';

	interface Props {
		id: number;
		psts: Pst[];
		onedit: (account: Account) => void;
		ondelete: (account: Account) => void;
		ondetails: (account: Account) => void;
		onupload: (id: number) => void;
		onroute: (account: Account) => void;
	}

	let { id, psts, onedit, ondelete, ondetails, onupload, onroute }: Props = $props();

	// A deleted row may still be fading out: it keeps showing what it last knew.
	let last: Account | undefined;
	const account = $derived.by(() => (last = data.accounts.get(id) ?? last) as Account);
	const email = $derived(prefs.email(account.email));
	const login = $derived(account.login && account.login !== account.email ? prefs.email(account.login) : '');
	const pstId = $derived(account.pst_id);
	const serverName = $derived(data.servers.find((s) => s.id === account.server_id)?.name ?? '');
	const selected = $derived(data.selected.has(id));
	const phase = $derived(data.testing.get(id));
	const mail = $derived(account.options.mail);
	const calendar = $derived(account.options.calendar);
	const contacts = $derived(account.options.contacts);
	const columns = $derived(prefs.value.columns);
	// Another source than the PST, or an own target: said in words, edited in the route dialog.
	const customSource = $derived(endpointSummary(connectors.list, account.source, 'source'));
	const sourceLabel = $derived(customSource ? customSource.label : '');
	const sourceDetail = $derived(customSource ? prefs.text(customSource.detail) : '');
	const targetLabel = $derived(endpointSummary(connectors.list, account.target, 'target')?.label ?? '');
	const canRoute = $derived(connectors.list.some((c) => c.source && c.kind !== 'pst') || connectors.list.some((c) => c.target));
	const options = $derived(psts.filter((p) => p.status === 'valid' || p.id === pstId));

	function onPstChange(e: Event & { currentTarget: HTMLSelectElement }) {
		const value = e.currentTarget.value;
		if (value === UPLOAD_VALUE) {
			e.currentTarget.value = String(pstId ?? '');
			onupload(id);
			return;
		}
		void data.setPst(id, value ? Number(value) : null);
	}

	function toggleSelected(e: Event & { currentTarget: HTMLInputElement }) {
		if (e.currentTarget.checked) data.selected.add(id);
		else data.selected.delete(id);
	}

	const CONTENT = $derived([
		{ key: 'mail' as const, on: mail, icon: Mail, label: m.migrations_row_content_mail() },
		{ key: 'calendar' as const, on: calendar, icon: Calendar, label: m.migrations_row_content_calendar() },
		{ key: 'contacts' as const, on: contacts, icon: Contact, label: m.migrations_row_content_contacts() }
	]);

	// A primitive: a progress event for this row does not rebuild the menu, a change of status does.
	const queued = $derived(data.jobs.get(id)?.status === 'queued');
	async function move(step: -1 | 1) {
		if (await data.moveQueued(id, step)) toast(step < 0 ? m.migrations_row_moved_up() : m.migrations_row_moved_down(), 'signal');
	}

	const menu = $derived([
		...(queued
			? [
					{ label: m.migrations_row_queue_up(), icon: ArrowUp, onselect: () => move(-1) },
					{ label: m.migrations_row_queue_down(), icon: ArrowDown, onselect: () => move(1) }
				]
			: []),
		{ label: m.migrations_row_edit(), icon: KeyRound, onselect: () => onedit(account) },
		...(canRoute || sourceLabel || targetLabel ? [{ label: m.migrations_row_route(), icon: Route, onselect: () => onroute(account) }] : []),
		{ label: m.migrations_row_delete(), icon: Trash2, danger: true, onselect: () => ondelete(account) }
	]);
</script>

<td class="sel">
	<label class="check">
		<input type="checkbox" checked={selected} aria-label={m.migrations_row_select({ email })} onchange={toggleSelected} />
	</label>
</td>
<td>
	<div class="truncate font-mono text-xs font-medium" title={email} data-testid="account-email">{email}</div>
	{#if login}<div class="truncate text-xs text-ink-3" title={login}>{m.migrations_row_login({ login })}</div>{/if}
</td>
{#if columns.server}
	<td class="cell-wide" data-label={m.migrations_col_server()}><div class="truncate" title={serverName}>{serverName}</div></td>
{/if}
{#if columns.connection}
	<td class="cell-wide" data-label={m.migrations_col_connection()}>
		<ConnectionCell {email} {phase} result={currentResult(account)} status={account.status} onclick={() => ondetails(account)} />
	</td>
{/if}
{#if columns.pst}
	<td class="cell-wide" data-label={m.migrations_col_pst()}>
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
		{#if targetLabel}<p class="m-0 mt-0.5 truncate text-xs text-ink-3" data-testid="target-summary">{m.migrations_row_target({ label: targetLabel })}</p>{/if}
	</td>
{/if}
{#if columns.content}
	<td class="cell-wide" data-label={m.migrations_col_content()}>
		<div class="flex gap-1">
			{#each CONTENT as item (item.key)}
				{@const Icon = item.icon}
				<button
					type="button"
					class={`inline-flex size-10 items-center justify-center rounded-btn border transition-colors duration-200 ${item.on ? 'border-line-strong bg-surface-2 text-ink' : 'border-dashed border-line text-ink-3 hover:text-ink-2'}`}
					aria-pressed={item.on}
					aria-label={m.migrations_row_content_toggle({ email, content: item.label })}
					title={item.label}
					onclick={() => data.setOption(id, item.key, !item.on)}
				>
					<Icon size={15} />
				</button>
			{/each}
		</div>
	</td>
{/if}
{#if columns.progress}
	<td class="cell-wide" data-label={m.migrations_col_progress()}>
		<ProgressCell accountId={id} />
	</td>
{/if}
<td class="has-menu">
	<div class="flex items-center justify-end">
		<IconButton
			icon={FlaskConical}
			label={phase ? m.migrations_row_testing({ email }) : m.migrations_row_test({ email })}
			loading={!!phase}
			onclick={() => data.test([id])}
		/>
		<Menu label={m.migrations_row_more({ email })} icon={EllipsisVertical} items={menu} />
	</div>
</td>
