<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FolderTree from '@lucide/svelte/icons/folder-tree';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { m } from '#lib/paraglide/messages.js';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtBytes, fmtNum } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { pstLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
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
	const linked = $derived(pst.status === 'valid' ? [...data.accounts.values()].filter((a) => a.pst_id === id).length : 0);

	const menu = $derived([
		...(pst.status === 'valid'
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
		{ label: m.files_list_delete(), icon: Trash2, danger: true, onselect: () => onremove(pst) }
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
		{#if totals}
			<p class="m-0 text-xs text-ink-2" data-testid="pst-totals">
				{m.files_list_totals({ mail: fmtNum(totals.mail ?? 0), calendar: fmtNum(totals.calendar ?? 0), contacts: fmtNum(totals.contacts ?? 0) })}
			</p>
		{/if}
		{#if pst.status === 'uploading'}<p class="m-0 text-xs text-advisory-ink">{m.files_list_incomplete()}</p>{/if}
		{#if pst.error}<p class="m-0 font-mono text-xs text-warning-ink">{pst.error}</p>{/if}
	</div>
	<div class="flex shrink-0 items-center">
		{#if pst.scan?.folders?.length}
			<button type="button" class="inline-flex min-h-10 items-center gap-1 rounded-btn px-2 text-ink-2 hover:text-ink" onclick={() => ondetails(pst)}>
				<FolderTree size={14} />{m.files_list_folders()}
			</button>
		{/if}
		<Menu label={m.files_list_more({ name })} icon={EllipsisVertical} items={menu} />
	</div>
</li>
