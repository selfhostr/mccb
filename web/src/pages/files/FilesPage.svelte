<script lang="ts">
	import FileArchive from '@lucide/svelte/icons/file-archive';
	import Server from '@lucide/svelte/icons/server';
	import Upload from '@lucide/svelte/icons/upload';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, del } from '../../lib/api/client.ts';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
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
	const totalSize = $derived(psts.reduce((sum, p) => sum + (p.size ?? 0), 0));

	function take(list: FileList | null | undefined) {
		const files = [...(list ?? [])];
		if (!files.length) return;
		const odd = files.some((f) => !/\.(pst|ost)$/i.test(f.name));
		// One plain PST goes straight to the queue: the order is only a question with several files.
		if (files.length === 1 && !odd) uploads.add(files as FileLike[], 'size-asc');
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

<PageHeader
	title={m.files_page_title()}
	sentence={data.loading ? m.common_loading() : psts.length ? m.files_page_summary({ total: psts.length, valid, size: fmtBytes(totalSize) }) : m.files_page_empty()}
/>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class={`mb-4 flex flex-col items-center gap-2 rounded-card border border-dashed px-4 py-6 text-center transition-colors duration-200 ${over ? 'border-ink bg-ghost' : 'border-line-strong'}`}
	ondragover={(e) => {
		e.preventDefault();
		over = true;
	}}
	ondragleave={() => (over = false)}
	ondrop={onDrop}
	data-testid="dropzone"
>
	<span class="text-ink-3"><Upload size={20} /></span>
	<p class="m-0 text-ink-2">{m.files_drop_sentence()}</p>
	<div class="flex flex-wrap justify-center gap-2">
		<Button variant="primary" icon={Upload} onclick={() => picker?.click()}>{m.files_drop_choose()}</Button>
		<Button icon={Server} onclick={() => (fetching = true)} data-testid="fetch-open">{m.files_fetch_open()}</Button>
	</div>
	<input
		bind:this={picker}
		class="sr-only"
		type="file"
		multiple
		tabindex="-1"
		aria-label={m.files_drop_choose()}
		onchange={(e) => {
			take(e.currentTarget.files);
			e.currentTarget.value = '';
		}}
		data-testid="file-input"
	/>
</div>

<InboxPanel />

<QueuePanel />

<h2 class="mt-6 mb-2 text-[0.9375rem]">{m.files_list_title()}</h2>
{#if data.error && !psts.length}
	<ErrorNotice title={m.files_list_load_failed()} message={data.error} onretry={() => data.load()} />
{:else if data.loading}
	<div class="flex flex-col gap-2">
		{#each [0, 1, 2] as i (i)}<Skeleton class="h-14" />{/each}
	</div>
{:else if !psts.length}
	<EmptyState icon={FileArchive} title={m.files_list_empty_title()} description={m.files_list_empty_text()} />
{:else}
	<ul class="m-0 flex list-none flex-col p-0" data-testid="pst-list">
		{#each psts as pst (pst.id)}
			<PstRow id={pst.id} ondetails={(p) => (details = p)} onremove={(p) => (removing = p)} />
		{/each}
	</ul>
{/if}

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
