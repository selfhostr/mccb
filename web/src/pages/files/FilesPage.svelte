<script lang="ts">
	import FileArchive from '@lucide/svelte/icons/file-archive';
	import Server from '@lucide/svelte/icons/server';
	import Upload from '@lucide/svelte/icons/upload';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, del } from '../../lib/api/client.ts';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { FILE_ACCEPT } from '../../lib/logic/pst.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import type { FileLike } from '../../lib/upload/queue.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import FetchDialog from './FetchDialog.svelte';
	import InboxPanel from './InboxPanel.svelte';
	import OrderDialog from './OrderDialog.svelte';
	import PstDialog from './PstDialog.svelte';
	import PstRow from './PstRow.svelte';
	import QueuePanel from './QueuePanel.svelte';

	let picker = $state<HTMLInputElement>();
	let over = $state(false);
	let pending = $state.raw<File[] | null>(null);
	let details = $state.raw<Pst | null>(null);
	let removing = $state.raw<Pst | null>(null);
	let fetching = $state(false);

	const psts = $derived([...data.psts.values()].sort((a, b) => b.id - a.id));
	const valid = $derived(psts.filter((p) => p.status === 'valid').length);
	const empty = $derived(!data.loading && !psts.length && !uploads.items.length);
	const totalSize = $derived(psts.reduce((sum, p) => sum + (p.size ?? 0), 0));

	function take(list: FileList | null | undefined) {
		const files = [...(list ?? [])];
		if (!files.length) return;
		// One file goes straight to the queue: the order is only a question with several.
		if (files.length === 1) uploads.add(files as FileLike[], 'size-asc');
		else pending = files;
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		over = false;
		take(e.dataTransfer?.files);
	}

	async function remove(pst: Pst) {
		try {
			await del(`/psts/${pst.id}`);
			data.psts.delete(pst.id);
			toast(m.files_list_deleted({ name: prefs.file(pst.filename) }), 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}
</script>

<PageHeader title={m.files_page_title()} sentence={psts.length ? m.files_page_summary({ total: psts.length, valid, size: fmtBytes(totalSize) }) : undefined}>
	{#snippet actions()}
		<IconButton variant="outline" icon={Server} label={m.files_fetch_open()} onclick={() => (fetching = true)} data-testid="fetch-open" />
		<IconButton variant="primary" icon={Upload} label={m.files_drop_choose()} onclick={() => picker?.click()} data-testid="file-choose" />
	{/snippet}
</PageHeader>

<div class="flex flex-col gap-4">
	<!-- The whole frame takes a drop and a click; with no file yet it is also the empty page. -->
	<button
		type="button"
		class={`flex w-full items-center justify-center gap-3 rounded-card border border-dashed px-4 text-ink-2 transition-colors duration-200 hover:border-ink-3 hover:text-ink ${empty ? 'min-h-48 flex-col' : 'min-h-14'} ${over ? 'border-ink bg-ghost text-ink' : 'border-line-strong'}`}
		ondragover={(e) => {
			e.preventDefault();
			over = true;
		}}
		ondragleave={() => (over = false)}
		ondrop={onDrop}
		onclick={() => picker?.click()}
		data-testid="dropzone"
	>
		<FileArchive size={empty ? 20 : 16} class="shrink-0" />
		<span class="min-w-0">{m.files_drop_sentence()}</span>
	</button>
	<input
		bind:this={picker}
		class="sr-only"
		type="file"
		multiple
		accept={FILE_ACCEPT}
		tabindex="-1"
		aria-label={m.files_drop_choose()}
		onchange={(e) => {
			take(e.currentTarget.files);
			e.currentTarget.value = '';
		}}
		data-testid="file-input"
	/>

	<InboxPanel />

	{#if data.error && !psts.length}
		<ErrorNotice title={m.files_list_load_failed()} message={data.error} onretry={() => data.load()} />
	{/if}

	{#if data.loading}
		<div class="flex flex-col gap-2">
			{#each [0, 1, 2] as i (i)}<Skeleton class="h-14" />{/each}
		</div>
	{:else if uploads.items.length || psts.length}
		<Panel class="row-panel">
			<QueuePanel />
			{#if psts.length}
				<ul class="row-list" aria-label={m.files_list_title()} data-testid="pst-list">
					{#each psts as pst (pst.id)}
						<PstRow id={pst.id} ondetails={(p) => (details = p)} onremove={(p) => (removing = p)} />
					{/each}
				</ul>
			{/if}
		</Panel>
	{/if}
</div>

{#if fetching}
	<FetchDialog onclose={() => (fetching = false)} />
{/if}
{#if pending}
	<OrderDialog files={pending} onclose={() => (pending = null)} />
{/if}
{#if details}
	<PstDialog pst={details} onclose={() => (details = null)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.files_delete_title()} text={m.files_delete_text({ name: prefs.file(target.filename) })} confirm={m.files_delete_confirm()} danger onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
