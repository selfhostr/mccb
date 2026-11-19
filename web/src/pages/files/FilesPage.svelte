<script lang="ts">
	import FileArchive from '@lucide/svelte/icons/file-archive';
	import SearchX from '@lucide/svelte/icons/search-x';
	import Server from '@lucide/svelte/icons/server';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import Upload from '@lucide/svelte/icons/upload';
	import X from '@lucide/svelte/icons/x';
	import { SvelteSet } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, del } from '../../lib/api/client.ts';
	import type { Pst, PstStatus } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { FILE_ACCEPT, isFetching } from '../../lib/logic/pst.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import type { FileLike } from '../../lib/upload/queue.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import FetchDialog from './FetchDialog.svelte';
	import FileMigrateDialog from './FileMigrateDialog.svelte';
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
	let migrating = $state.raw<Pst | null>(null);
	/** Several files to delete at once: the checked ones, or the invalid ones. */
	let bulk = $state.raw<{ ids: number[]; invalid: boolean } | null>(null);

	type Filter = '' | 'valid' | 'pending' | 'failed';
	const GROUP: Record<PstStatus, Exclude<Filter, ''>> = {
		valid: 'valid',
		uploading: 'pending',
		validating: 'pending',
		fetching: 'pending',
		cancelling: 'pending',
		invalid: 'failed',
		fetch_failed: 'failed'
	};
	let filter = $state<Filter>('');
	const checked = new SvelteSet<number>();

	const psts = $derived([...data.psts.values()].sort((a, b) => b.id - a.id));
	const valid = $derived(psts.filter((p) => p.status === 'valid').length);
	const empty = $derived(!data.loading && !psts.length && !uploads.items.length);
	const totalSize = $derived(psts.reduce((sum, p) => sum + (p.size ?? 0), 0));
	const groupOf = (p: Pst) => GROUP[p.status] ?? 'pending';
	const counts = $derived.by(() => {
		const out = { valid: 0, pending: 0, failed: 0 };
		for (const p of psts) out[groupOf(p)]++;
		return out;
	});
	const FILTERS = $derived([
		{ value: '' as const, label: m.files_filter_all(), count: psts.length },
		{ value: 'valid' as const, label: m.files_filter_valid(), count: counts.valid },
		{ value: 'pending' as const, label: m.files_filter_pending(), count: counts.pending },
		{ value: 'failed' as const, label: m.files_filter_failed(), count: counts.failed }
	]);
	const shown = $derived(filter ? psts.filter((p) => groupOf(p) === filter) : psts);
	/** A download in progress cannot be deleted: it is never checked. */
	const deletable = (p: Pst) => !isFetching(p);
	const shownIds = $derived(shown.filter(deletable).map((p) => p.id));
	/** What is checked and still deletable: a file that went away or started downloading drops out. */
	const targets = $derived([...checked].filter((id) => {
		const p = data.psts.get(id);
		return p != null && deletable(p);
	}));
	const header = $derived.by(() => {
		const n = shownIds.filter((id) => checked.has(id)).length;
		return n === 0 ? 'none' : n === shownIds.length ? 'all' : 'some';
	});
	const failedIds = $derived(psts.filter((p) => groupOf(p) === 'failed' && deletable(p)).map((p) => p.id));

	function toggleAll() {
		if (header === 'all') for (const id of shownIds) checked.delete(id);
		else for (const id of shownIds) checked.add(id);
	}

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

	async function removeMany(ids: number[]) {
		let deleted = 0;
		const errors: string[] = [];
		for (const id of ids) {
			try {
				await del(`/psts/${id}`);
				data.psts.delete(id);
				checked.delete(id);
				deleted++;
			} catch (error) {
				errors.push(errorText(error));
			}
		}
		if (deleted) toast(m.files_bulk_deleted({ count: deleted }), 'signal');
		if (errors.length) toast(m.files_bulk_failed({ count: errors.length, error: errors[0] }), 'warning');
	}

	async function remove(pst: Pst) {
		try {
			await del(`/psts/${pst.id}`);
			data.psts.delete(pst.id);
			checked.delete(pst.id);
			toast(m.files_list_deleted({ name: prefs.file(pst.filename) }), 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}
</script>

<PageHeader title={m.files_page_title()} sentence={psts.length ? m.files_page_summary({ total: psts.length, valid, size: fmtBytes(totalSize) }) : undefined}>
	{#snippet actions()}
		<Button icon={Server} onclick={() => (fetching = true)} data-testid="fetch-open">{m.files_fetch_open()}</Button>
		<Button variant="primary" icon={Upload} onclick={() => picker?.click()} data-testid="file-choose">{m.files_drop_upload()}</Button>
	{/snippet}
</PageHeader>

<div class={`flex flex-col gap-4 ${targets.length ? 'pb-20' : ''}`}>
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
		{#if psts.length}
			<div class="flex flex-wrap items-center gap-1.5">
				<div class="flex flex-wrap items-center gap-1.5" role="group" aria-label={m.files_filter_label()} data-testid="pst-filters">
					{#each FILTERS as chip (chip.value)}
						<button
							type="button"
							class={`inline-flex min-h-10 items-center gap-1.5 rounded-btn border px-3 font-medium transition-colors duration-200 ${filter === chip.value ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
							aria-pressed={filter === chip.value}
							onclick={() => (filter = chip.value)}
							data-testid={`pst-filter-${chip.value || 'all'}`}
						>
							{chip.label}
							<span class="font-mono text-ink-2">{chip.count}</span>
						</button>
					{/each}
				</div>
				{#if failedIds.length}
					<span class="ms-auto"><Button variant="ghost" icon={Trash2} onclick={() => (bulk = { ids: failedIds, invalid: true })} data-testid="pst-remove-invalid">{m.files_remove_invalid()}</Button></span>
				{/if}
			</div>
		{/if}
		<Panel class="row-panel">
			<QueuePanel />
			{#if psts.length}
				{#if shownIds.length}
					<div class="row-head border-t border-line first:border-t-0" data-testid="pst-head">
						<label class="check">
							<input type="checkbox" checked={header === 'all'} indeterminate={header === 'some'} onchange={toggleAll} data-testid="pst-select-all" />
							<span class="whitespace-nowrap">{m.files_list_select_all()}</span>
						</label>
					</div>
				{/if}
				{#if shown.length}
					<ul class="row-list" aria-label={m.files_list_title()} data-testid="pst-list">
						{#each shown as pst (pst.id)}
							<PstRow
								id={pst.id}
								selected={checked.has(pst.id)}
								onselect={(on) => (on ? checked.add(pst.id) : checked.delete(pst.id))}
								ondetails={(p) => (details = p)}
								onremove={(p) => (removing = p)}
								onmigrate={(p) => (migrating = p)}
							/>
						{/each}
					</ul>
				{:else}
					<EmptyState icon={SearchX} title={m.files_list_none()}>
						{#snippet action()}
							<Button onclick={() => (filter = '')}>{m.files_list_show_all()}</Button>
						{/snippet}
					</EmptyState>
				{/if}
			{/if}
		</Panel>
	{/if}
</div>

{#if targets.length}
	<div class="fixed right-0 bottom-0 left-[var(--side-w)] z-30 border-t border-line-strong bg-surface" role="region" aria-label={m.files_bar_label()} data-testid="pst-bar">
		<div class="mx-auto flex max-w-[1160px] flex-wrap items-center gap-x-3 gap-y-1 px-[var(--gutter)] py-2">
			<p class="m-0 min-w-0 flex-1 basis-56 font-medium" aria-live="polite" data-testid="pst-bar-count">{m.files_bar_count({ count: targets.length })}</p>
			<div class="flex flex-wrap items-center gap-2">
				<Button variant="danger" icon={Trash2} onclick={() => (bulk = { ids: targets, invalid: false })} data-testid="pst-bulk-delete">{m.files_list_delete()}</Button>
				<IconButton icon={X} label={m.files_bar_clear()} onclick={() => checked.clear()} />
			</div>
		</div>
	</div>
{/if}

{#if migrating}
	<FileMigrateDialog pst={migrating} onclose={() => (migrating = null)} />
{/if}
{#if bulk}
	{@const job = bulk}
	<Confirm
		title={job.invalid ? m.files_remove_invalid_title() : m.files_bulk_title()}
		text={job.invalid ? m.files_remove_invalid_text({ count: job.ids.length }) : m.files_bulk_text({ count: job.ids.length })}
		confirm={m.files_bulk_confirm({ count: job.ids.length })}
		danger
		onconfirm={() => removeMany(job.ids)}
		onclose={() => (bulk = null)}
	/>
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
