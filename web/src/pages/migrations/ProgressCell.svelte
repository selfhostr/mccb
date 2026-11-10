<script lang="ts">
	/** Its own component: a progress event for this account runs this and nothing else in the row. */
	import { m } from '#lib/paraglide/messages.js';
	import { jobPercent } from '../../lib/logic/accounts.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Progress from '../../lib/ui/Progress.svelte';

	let { accountId }: { accountId: number } = $props();

	const job = $derived(data.jobs.get(accountId));
	const percent = $derived(jobPercent(job));
</script>

<!-- Before a first job there is nothing to measure: an empty track and "0%" on every row would only be noise. -->
{#if job}
	<span class="flex items-center gap-3">
		<span class="min-w-0 flex-1"><Progress value={percent} label={m.migrations_col_progress()} /></span>
		<span class="w-11 shrink-0 text-right font-mono text-xs text-ink-2">{percent}%</span>
	</span>
{/if}
