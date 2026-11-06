<script lang="ts">
	/** Its own component: a progress event for this account runs this and nothing else in the row. */
	import { m } from '#lib/paraglide/messages.js';
	import { jobPercent } from '../../lib/logic/accounts.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Progress from '../../lib/ui/Progress.svelte';

	let { accountId }: { accountId: number } = $props();

	const percent = $derived(jobPercent(data.jobs.get(accountId)));
</script>

<span class="flex items-center gap-2.5">
	<span class="min-w-0 flex-1"><Progress value={percent} label={m.migrations_col_progress()} /></span>
	<span class="w-11 shrink-0 text-right font-mono text-xs text-ink-2">{percent}%</span>
</span>
