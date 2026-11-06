<script lang="ts">
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { JobError } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';

	let { jobId, refreshKey }: { jobId: number; refreshKey: number } = $props();

	let list = $state.raw<JobError[] | null>(null);
	let error = $state<string | null>(null);

	async function load() {
		error = null;
		try {
			list = await get<JobError[]>(`/jobs/${jobId}/errors`);
		} catch (e) {
			error = errorText(e);
		}
	}

	$effect(() => {
		void refreshKey;
		void load();
	});
</script>

{#if error}
	<ErrorNotice title={m.jobs_errors_failed()} message={error} onretry={load} />
{:else if !list}
	<Skeleton class="h-32" />
{:else if !list.length}
	<EmptyState icon={CircleCheck} title={m.jobs_errors_none()} />
{:else}
	<ul class="m-0 flex list-none flex-col gap-2 p-0" data-testid="job-errors">
		{#each list.slice(0, 500) as item (item.id ?? item.item_key)}
			<li class="border-b border-line pb-2">
				<p class="m-0 font-medium">{prefs.text(item.subject || item.item_key) || m.jobs_errors_item()}</p>
				<p class="m-0 text-xs text-ink-3">{item.kind}{item.folder ? ` / ${item.folder}` : ''}</p>
				<p class="m-0 font-mono text-xs text-warning-ink">{prefs.text(item.error)}</p>
			</li>
		{/each}
	</ul>
{/if}
