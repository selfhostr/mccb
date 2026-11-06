<script lang="ts">
	/** Items a job did not copy on purpose: left out by a filter, or too large for the target. Loaded when opened. */
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { LeftOutItem } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';

	let { jobId, what, title }: { jobId: number; what: 'filtered' | 'oversized'; title: string } = $props();

	const SHOWN = 50;
	let open = $state(false);
	let list = $state.raw<LeftOutItem[] | null>(null);
	let error = $state<string | null>(null);

	async function load() {
		error = null;
		try {
			list = await get<LeftOutItem[]>(`/jobs/${jobId}/${what}?limit=${SHOWN * 2}`);
		} catch (e) {
			error = errorText(e);
		}
	}

	$effect(() => {
		if (open && !list && !error) void load();
	});

	const reasonOf = (item: LeftOutItem) => (what === 'oversized' ? fmtBytes(item.size) : (item.reason ?? ''));
</script>

<Disclosure {title} summary={list ? String(list.length) : undefined} bind:open data-testid={`leftout-${what}`}>
	{#if error}
		<ErrorNotice title={m.leftout_failed()} message={error} onretry={load} />
	{:else if !list}
		<Skeleton class="h-16" />
	{:else if !list.length}
		<p class="m-0 text-ink-3">{m.leftout_none()}</p>
	{:else}
		<ul class="m-0 flex list-none flex-col gap-1 p-0 text-xs">
			{#each list.slice(0, SHOWN) as item (item.id)}
				<li class="border-b border-line pb-1">
					<span class="font-medium">{prefs.text(item.subject || item.item_key) || m.jobs_errors_item()}</span>
					<span class="block text-ink-3">{item.kind}{item.folder ? ` / ${prefs.text(item.folder)}` : ''}{reasonOf(item) ? `, ${reasonOf(item)}` : ''}</span>
				</li>
			{/each}
		</ul>
		{#if list.length > SHOWN}<p class="m-0 mt-1 text-xs text-ink-3">{m.leftout_more({ count: list.length - SHOWN })}</p>{/if}
	{/if}
</Disclosure>
