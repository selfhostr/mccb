<script lang="ts">
	/** Three streams, as in the logo: mail, calendar and contacts, each with its own thin track. */
	import { m } from '#lib/paraglide/messages.js';
	import type { Job, StreamProgress } from '../../lib/api/types.ts';
	import { pct } from '../../lib/format.ts';
	import Progress from '../../lib/ui/Progress.svelte';

	let { job }: { job: Job } = $props();

	const STREAMS = $derived([
		{ key: 'mail', label: m.migrations_row_content_mail(), p: job.progress?.mail },
		{ key: 'calendar', label: m.migrations_row_content_calendar(), p: job.progress?.calendar },
		{ key: 'contacts', label: m.migrations_row_content_contacts(), p: job.progress?.contacts }
	]);

	function tone(p: StreamProgress | undefined, key: string) {
		if (job.status === 'failed') return 'warning' as const;
		if (p?.errors) return 'advisory' as const;
		if (job.status === 'done') return 'signal' as const;
		return job.phase === key && job.status === 'running' ? ('ink' as const) : ('muted' as const);
	}
</script>

<div class="flex flex-col gap-0.5">
	{#each STREAMS as stream (stream.key)}
		<Progress value={pct(stream.p?.done ?? 0, stream.p?.total ?? 0)} tone={tone(stream.p, stream.key)} label={stream.label} height="h-[3px]" />
	{/each}
</div>
