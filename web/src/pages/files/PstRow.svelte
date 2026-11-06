<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FolderTree from '@lucide/svelte/icons/folder-tree';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtBytes, fmtNum } from '../../lib/format.ts';
	import { canPreview, chainView, fetchPercent, formatOf, isFetching, kindOf } from '../../lib/logic/pst.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { pstLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';

	interface Props {
		id: number;
		ondetails: (pst: Pst) => void;
		onremove: (pst: Pst) => void;
	}

	let { id, ondetails, onremove }: Props = $props();

	let last: Pst | undefined;
	const pst = $derived.by(() => (last = data.psts.get(id) ?? last) as Pst);
	const look = $derived(pstLook(pst.status));
	const name = $derived(prefs.file(pst.filename));
	const totals = $derived(pst.scan?.totals);
	const kind = $derived(formatOf(pst.scan) || kindOf(pst.filename));
	const linked = $derived(pst.status === 'valid' ? [...data.accounts.values()].filter((a) => a.pst_id === id).length : 0);

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
		...(fetching ? [] : [{ label: m.files_list_delete(), icon: Trash2, danger: true, onselect: () => onremove(pst) }])
	]);
</script>

<li class="file-row" data-pst={pst.filename}>
	<div class="file-name">
		<p class="m-0 truncate font-semibold" title={name}>{name}</p>
		{#if totals}
			<p class="m-0 text-xs text-ink-2" data-testid="pst-totals">
				{m.files_list_totals({ mail: fmtNum(totals.mail ?? 0), calendar: fmtNum(totals.calendar ?? 0), contacts: fmtNum(totals.contacts ?? 0) })}
			</p>
		{/if}
		{#if pst.created_at || linked}
			<p class="m-0 text-xs text-ink-3">
				{#if pst.created_at}<RelTime value={pst.created_at} />{/if}
				{#if linked}<span> {m.files_list_linked({ count: linked })}</span>{/if}
			</p>
		{/if}
		{#if from}<p class="m-0 text-xs text-ink-3" data-testid="pst-from">{from}</p>{/if}
		{#if fetching}
			<p class="m-0 text-xs text-ink-3" data-testid="pst-fetch">
				{percent == null ? m.files_row_fetch_received({ received }) : m.files_row_fetch_progress({ received, size: fmtBytes(pst.size), percent })}
			</p>
		{/if}
		{#if pst.status === 'uploading'}<p class="m-0 text-xs text-advisory-ink">{m.files_list_incomplete()}</p>{/if}
		{#if chain}
			<p class={`m-0 text-xs ${chain.code === 'error' || chain.code === 'ambiguous' ? 'text-advisory-ink' : 'text-ink-2'}`} data-testid="pst-chain">
				{#if chain.code === 'assigned'}{chain.started ? m.files_chain_assigned_started({ email: chainEmail }) : m.files_chain_assigned({ email: chainEmail })}
				{:else if chain.code === 'ambiguous'}{m.files_chain_ambiguous()}
				{:else if chain.code === 'no_match'}{m.files_chain_no_match()}
				{:else if chain.code === 'account_has_pst'}{m.files_chain_account_has_pst()}
				{:else if chain.code === 'already_assigned'}{m.files_chain_already_assigned()}
				{:else if chain.code === 'error'}{m.files_chain_error({ detail: chain.detail })}
				{:else}{m.files_chain_other({ result: chain.detail })}{/if}
			</p>
		{/if}
		{#if pst.error}<p class="m-0 font-mono text-xs text-warning-ink">{pst.error}</p>{/if}
	</div>
	<span class="file-kind text-xs text-ink-2" title={kind} data-testid="pst-format">{kind}</span>
	<span class="file-size text-xs text-ink-2">{fmtBytes(pst.size)}</span>
	<div class="file-status"><Plate tone={look.tone} icon={look.icon} label={look.label} /></div>
	<div class="file-bar flex items-center gap-2.5">
		{#if fetching}
			<span class="min-w-0 flex-1"><Progress value={percent ?? 0} label={name} /></span>
			{#if pst.status === 'fetching'}
				<Button variant="ghost" icon={X} onclick={() => data.cancelFetch(id)} aria-label={m.files_row_cancel({ name })} data-testid="fetch-cancel">{m.common_cancel()}</Button>
			{/if}
		{:else if pst.status === 'fetch_failed'}
			<Button icon={RotateCw} onclick={() => data.retryFetch(id)} aria-label={m.files_row_retry({ name })} data-testid="fetch-retry">{m.common_retry()}</Button>
		{/if}
	</div>
	<div class="file-menu">
		{#if menu.length}<Menu label={m.files_list_more({ name })} icon={EllipsisVertical} items={menu} />{/if}
	</div>
</li>
