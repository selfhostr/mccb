<script lang="ts">
	/** Its own component: a progress event for this account runs this and nothing else in the row. */
	import { m } from '#lib/paraglide/messages.js';
	import { DASH, fmtBytes } from '../../lib/format.ts';
	import { importedBytes, jobPercent } from '../../lib/logic/accounts.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Progress from '../../lib/ui/Progress.svelte';
	import SizeTotal from './SizeTotal.svelte';

	let { accountId }: { accountId: number } = $props();

	const job = $derived(data.jobs.get(accountId));
	const percent = $derived(jobPercent(job));
	const size = $derived(data.accounts.get(accountId)?.source_size);
	const imported = $derived(importedBytes(size, job));
	// Before a job, the size alone, once it is known or being measured.
	const showSize = $derived(!!job || size?.bytes != null || !!size?.computing);
	const title = $derived(job ? m.migrations_size_of_total({ imported: fmtBytes(imported), total: size?.bytes != null ? `${size.approx ? '≈ ' : ''}${fmtBytes(size.bytes)}` : DASH }) : m.migrations_size_source());
</script>

<!-- Before a first job there is nothing to measure: an empty track and "0%" on every row would only be noise. -->
{#if job}
	<span class="flex items-center gap-3">
		<span class="min-w-0 flex-1"><Progress value={percent} label={m.migrations_col_progress()} /></span>
		<span class="w-11 shrink-0 text-right font-mono text-xs text-ink-2">{percent}%</span>
	</span>
{/if}
{#if showSize}
	<span class="mt-1 block truncate font-mono text-xs text-ink-2" data-testid="row-size" {title}>
		{#if job}{`${fmtBytes(imported)} / `}{/if}<SizeTotal {size} />
	</span>
{/if}
