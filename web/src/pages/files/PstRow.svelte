<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FolderTree from '@lucide/svelte/icons/folder-tree';
	import Send from '@lucide/svelte/icons/send';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtBytes, fmtNum } from '../../lib/format.ts';
	import { canPreview, chainView, fetchPercent, formatOf, isFetching, kindOf } from '../../lib/logic/pst.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { pstLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';

	interface Props {
		id: number;
		selected: boolean;
		onselect: (on: boolean) => void;
		ondetails: (pst: Pst) => void;
		onremove: (pst: Pst) => void;
		onmigrate: (pst: Pst) => void;
	}

	let { id, selected, onselect, ondetails, onremove, onmigrate }: Props = $props();

	let last: Pst | undefined;
	const pst = $derived.by(() => (last = data.psts.get(id) ?? last) as Pst);
	const look = $derived(pstLook(pst.status));
	const name = $derived(prefs.file(pst.filename));
	const totals = $derived(pst.scan?.totals);
	const kind = $derived(formatOf(pst.scan) || kindOf(pst.filename));
	const linked = $derived(pst.status === 'valid' ? [...data.accounts.values()].filter((a) => a.pst_id === id).map((a) => a.id) : []);

	const fetching = $derived(isFetching(pst));
	const percent = $derived(fetchPercent(pst));
	const received = $derived(fmtBytes(pst.received_bytes ?? 0));
	const origin = $derived(prefs.text(pst.origin ?? ''));
	const from = $derived(pst.source === 'inbox' ? m.files_row_from_inbox() : pst.source === 'fetch' ? (origin ? m.files_row_from_fetch({ origin }) : m.files_row_from_fetch_plain()) : '');
	const chain = $derived(chainView(pst.chain, (accountId) => data.accounts.get(accountId)));
	const chainEmail = $derived(chain?.accountEmail ? prefs.email(chain.accountEmail) : chain?.accountId != null ? `#${chain.accountId}` : '');

	const menu = $derived([
		...(pst.scan?.folders?.length ? [{ label: m.files_list_folders(), icon: FolderTree, onselect: () => ondetails(pst) }] : []),
		...(canPreview(pst)
			? [
					{
						label: m.files_list_preview(),
						icon: Download,
						onselect: () => {
							const link = document.createElement('a');
							link.href = `/api/psts/${id}/preview?limit=20`;
							link.download = '';
							link.click();
						}
					}
				]
			: []),
		// A download in progress cannot be deleted: the menu says so instead of hiding the action.
		fetching ? { label: m.files_list_delete_fetching(), icon: Trash2, disabled: true, onselect: () => {} } : { label: m.files_list_delete(), icon: Trash2, danger: true, onselect: () => onremove(pst) }
	]);

	/** Several linked mailboxes open as a selection on the Migrations page: the bar offers to start them. */
	function showLinked(e: MouseEvent) {
		if (linked.length < 2) return;
		e.preventDefault();
		data.selected.clear();
		for (const accountId of linked) data.selected.add(accountId);
		router.go('/');
	}
</script>

<li class={`file-row ${selected ? 'bg-ghost' : ''}`} data-pst={pst.filename}>
	<div class="file-name flex items-start gap-1">
		<label class="check -my-2 -ms-3 shrink-0">
			<input type="checkbox" checked={selected} disabled={fetching} aria-label={m.files_list_select({ name })} onchange={(e) => onselect(e.currentTarget.checked)} data-testid="pst-select" />
		</label>
		<div class="min-w-0 flex-1">
			<p class="m-0 truncate font-medium" title={name}>{name}</p>
			{#if totals || pst.created_at || from}
				<p class="m-0 flex flex-wrap gap-x-3 text-xs text-ink-2">
					{#if totals}<span data-testid="pst-totals">{m.files_list_totals({ mail: fmtNum(totals.mail ?? 0), calendar: fmtNum(totals.calendar ?? 0), contacts: fmtNum(totals.contacts ?? 0) })}</span>{/if}
					{#if pst.created_at}<span class="text-ink-3"><RelTime value={pst.created_at} /></span>{/if}
					{#if from}<span class="text-ink-3" data-testid="pst-from">{from}</span>{/if}
				</p>
			{/if}
			{#if fetching || pst.status === 'uploading' || pst.error || chain || linked.length}
				<p class="m-0 flex flex-wrap gap-x-3 text-xs text-ink-2">
					{#if fetching}
						<span class="text-ink-3" data-testid="pst-fetch">{percent == null ? m.files_row_fetch_received({ received }) : m.files_row_fetch_progress({ received, size: fmtBytes(pst.size), percent })}</span>
					{:else if pst.status === 'uploading'}
						<span class="text-advisory-ink">{m.files_list_incomplete()}</span>
					{/if}
					{#if pst.error}<span class="font-mono text-warning-ink">{pst.error}</span>{/if}
					{#if linked.length}
						<a class="text-ink-2 underline hover:text-ink" href={linked.length === 1 ? `/?account=${linked[0]}` : '/'} title={m.files_list_linked_open()} onclick={showLinked} data-testid="pst-linked">{m.files_list_linked({ count: linked.length })}</a>
					{/if}
					{#if chain && !linked.length}
						<span class={chain.code === 'error' || chain.code === 'ambiguous' || chain.code === 'no_match' || chain.code === 'account_has_pst' ? 'text-advisory-ink' : ''} data-testid="pst-chain">
							{#if chain.code === 'assigned'}{chain.started ? m.files_chain_assigned_started({ email: chainEmail }) : m.files_chain_assigned({ email: chainEmail })}
							{:else if chain.code === 'ambiguous'}{m.files_chain_ambiguous()}
							{:else if chain.code === 'no_match'}{m.files_chain_no_match()}
							{:else if chain.code === 'account_has_pst'}{m.files_chain_account_has_pst()}
							{:else if chain.code === 'already_assigned'}{m.files_chain_already_assigned()}
							{:else if chain.code === 'error'}{m.files_chain_error({ detail: chain.detail })}
							{:else}{m.files_chain_other({ result: chain.detail })}{/if}
						</span>
					{/if}
				</p>
			{/if}
		</div>
	</div>
	<span class="file-kind text-xs text-ink-2" title={kind} data-testid="pst-format">{kind}</span>
	<span class="file-size text-xs text-ink-2">{fmtBytes(pst.size)}</span>
	<div class="file-status"><Plate tone={look.tone} icon={look.icon} label={look.label} /></div>
	<div class="file-bar flex items-center gap-3">
		{#if fetching}
			<span class="min-w-0 flex-1"><Progress value={percent ?? 0} label={name} /></span>
			{#if pst.status === 'fetching'}
				<Button variant="ghost" icon={X} onclick={() => data.cancelFetch(id)} aria-label={m.files_row_cancel({ name })} data-testid="fetch-cancel">{m.common_cancel()}</Button>
			{/if}
		{:else if pst.status === 'fetch_failed'}
			<Button icon={RotateCw} onclick={() => data.retryFetch(id)} aria-label={m.files_row_retry({ name })} data-testid="fetch-retry">{m.common_retry()}</Button>
		{:else if pst.status === 'valid'}
			<Button icon={Send} onclick={() => onmigrate(pst)} aria-label={m.files_row_migrate_label({ name })} data-testid="pst-migrate">{m.files_row_migrate()}</Button>
		{/if}
	</div>
	<div class="file-menu">
		<Menu label={m.files_list_more({ name })} icon={EllipsisVertical} items={menu} />
	</div>
</li>
