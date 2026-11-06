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
	import { canPreview, chainView, fetchPercent, formatOf, isFetching } from '../../lib/logic/pst.ts';
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
	const format = $derived(formatOf(pst.scan));
	const linked = $derived(pst.status === 'valid' ? [...data.accounts.values()].filter((a) => a.pst_id === id).length : 0);

	const fetching = $derived(isFetching(pst));
	const percent = $derived(fetchPercent(pst));
	const received = $derived(fmtBytes(pst.received_bytes ?? 0));
	const origin = $derived(prefs.text(pst.origin ?? ''));
	const from = $derived(pst.source === 'inbox' ? m.files_row_from_inbox() : pst.source === 'fetch' ? (origin ? m.files_row_from_fetch({ origin }) : m.files_row_from_fetch_plain()) : '');
	const chain = $derived(chainView(pst.chain, (accountId) => data.accounts.get(accountId)));
	const chainEmail = $derived(chain?.accountEmail ? prefs.email(chain.accountEmail) : chain?.accountId != null ? `#${chain.accountId}` : '');

	const menu = $derived([
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

<li class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2" data-pst={pst.filename}>
	<div class="min-w-0 flex-1 basis-56">
		<div class="flex items-center gap-2">
			<span class="truncate font-mono text-xs font-medium" title={name}>{name}</span>
			<Plate tone={look.tone} icon={look.icon} label={look.label} class="shrink-0" />
		</div>
		<p class="m-0 text-xs text-ink-3">
			{fmtBytes(pst.size)}
			{#if pst.created_at}<span> <RelTime value={pst.created_at} /></span>{/if}
			{#if linked}<span> {m.files_list_linked({ count: linked })}</span>{/if}
		</p>
		{#if from}<p class="m-0 text-xs text-ink-3" data-testid="pst-from">{from}</p>{/if}
		{#if fetching}
			<div class="my-1"><Progress value={percent ?? 0} label={name} /></div>
			<p class="m-0 text-xs text-ink-3" data-testid="pst-fetch">
				{percent == null ? m.files_row_fetch_received({ received }) : m.files_row_fetch_progress({ received, size: fmtBytes(pst.size), percent })}
			</p>
		{/if}
		{#if totals}
			<p class="m-0 text-xs text-ink-2" data-testid="pst-totals">
				{m.files_list_totals({ mail: fmtNum(totals.mail ?? 0), calendar: fmtNum(totals.calendar ?? 0), contacts: fmtNum(totals.contacts ?? 0) })}
			</p>
		{/if}
		{#if format}<p class="m-0 text-xs text-ink-3" data-testid="pst-format">{m.files_row_format({ format })}</p>{/if}
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
	<div class="flex shrink-0 items-center">
		{#if pst.status === 'fetching'}
			<Button variant="ghost" icon={X} onclick={() => data.cancelFetch(id)} aria-label={m.files_row_cancel({ name })} data-testid="fetch-cancel">{m.common_cancel()}</Button>
		{:else if pst.status === 'fetch_failed'}
			<Button icon={RotateCw} onclick={() => data.retryFetch(id)} aria-label={m.files_row_retry({ name })} data-testid="fetch-retry">{m.common_retry()}</Button>
		{/if}
		{#if pst.scan?.folders?.length}
			<button type="button" class="inline-flex min-h-10 items-center gap-1 rounded-btn px-2 text-ink-2 hover:text-ink" onclick={() => ondetails(pst)}>
				<FolderTree size={14} />{m.files_list_folders()}
			</button>
		{/if}
		{#if menu.length}<Menu label={m.files_list_more({ name })} icon={EllipsisVertical} items={menu} />{/if}
	</div>
</li>
