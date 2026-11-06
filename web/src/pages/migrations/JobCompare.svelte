<script lang="ts">
	/** Source against target, folder by folder. The worker does the comparison: this asks, then waits for it. */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import Download from '@lucide/svelte/icons/download';
	import GitCompareArrows from '@lucide/svelte/icons/git-compare-arrows';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { onDestroy, onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get, post } from '../../lib/api/client.ts';
	import type { Compare } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';

	let { jobId, finished }: { jobId: number; finished: boolean } = $props();

	let compare = $state.raw<Compare | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setTimeout> | undefined;
	let gone = false;
	onDestroy(() => {
		gone = true;
		clearTimeout(timer);
	});

	const waiting = $derived(compare?.status === 'queued' || compare?.status === 'running');
	const result = $derived(compare?.status === 'done' ? compare.result : null);

	function follow() {
		clearTimeout(timer);
		if (gone || !waiting) return;
		timer = setTimeout(async () => {
			try {
				compare = await get<Compare>(`/jobs/${jobId}/compare`);
			} catch (e) {
				error = errorText(e);
				return;
			}
			follow();
		}, 1500);
	}

	async function load() {
		error = null;
		try {
			compare = await get<Compare>(`/jobs/${jobId}/compare`);
			follow();
		} catch (e) {
			error = errorText(e);
		}
	}

	async function start() {
		error = null;
		try {
			compare = await post<Compare>(`/jobs/${jobId}/compare`, {});
			follow();
		} catch (e) {
			error = errorText(e);
		}
	}

	onMount(() => void load());
</script>

<section class="flex flex-col gap-2" data-testid="job-compare">
	<div class="flex flex-wrap items-center gap-2">
		<Button icon={GitCompareArrows} loading={waiting} disabled={!finished} onclick={start} data-testid="compare-start">{m.compare_start()}</Button>
		{#if result}
			<Button variant="ghost" icon={Download} href={`/api/jobs/${jobId}/compare/csv`} download={`compare-job-${jobId}.csv`}>{m.compare_csv()}</Button>
		{/if}
	</div>
	{#if error}<ErrorNotice title={m.compare_failed()} message={error} onretry={load} />{/if}
	{#if compare?.status === 'failed'}<ErrorNotice title={m.compare_failed()} message={compare.error || m.compare_failed()} onretry={start} />{/if}
	{#if result}
		<Plate
			tone={result.ok && !result.totals.missing ? 'signal' : 'warning'}
			icon={result.ok && !result.totals.missing ? CircleCheck : TriangleAlert}
			label={result.ok && !result.totals.missing ? m.compare_ok() : m.compare_missing({ count: fmtNum(result.totals.missing ?? 0) })}
			class="self-start"
		/>
		<div class="overflow-x-auto">
			<table class="w-full text-xs" data-testid="compare-table">
				<thead>
					<tr class="text-left text-ink-2">
						<th class="py-1 pr-2 font-medium">{m.compare_col_folder()}</th>
						<th class="py-1 pr-2 text-right font-medium">{m.compare_col_expected()}</th>
						<th class="py-1 pr-2 text-right font-medium">{m.compare_col_on_target()}</th>
						<th class="py-1 text-right font-medium">{m.compare_col_missing()}</th>
					</tr>
				</thead>
				<tbody class="font-mono">
					{#each result.folders as folder, i (i)}
						<tr class="border-t border-line">
							<td class="py-1 pr-2 font-sans break-words">
								{prefs.text(folder.target || folder.folders.join(', '))}
								{#if folder.error}<span class="block font-mono text-warning-ink">{prefs.text(folder.error)}</span>{/if}
							</td>
							<td class="py-1 pr-2 text-right">{fmtNum(folder.expected)}</td>
							<td class="py-1 pr-2 text-right">{fmtNum(folder.on_target)}</td>
							<td class={`py-1 text-right ${folder.missing ? 'text-warning-ink' : ''}`}>{fmtNum(folder.missing)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>
