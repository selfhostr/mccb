<script lang="ts">
	import Minus from '@lucide/svelte/icons/minus';
	import { m } from '#lib/paraglide/messages.js';
	import { pct } from '../../lib/format.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { jobLookOf } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Plate from '../../lib/ui/Plate.svelte';
	import StreamBars from './StreamBars.svelte';

	let { accountId }: { accountId: number } = $props();

	const job = $derived(data.jobs.get(accountId));
	const look = $derived(job ? jobLookOf(job) : null);
	const percent = $derived.by(() => {
		if (!job?.progress) return 0;
		const { mail, calendar, contacts } = job.progress;
		return pct((mail?.done ?? 0) + (calendar?.done ?? 0) + (contacts?.done ?? 0), (mail?.total ?? 0) + (calendar?.total ?? 0) + (contacts?.total ?? 0));
	});
</script>

{#if job && look}
	<a class="block min-h-10 rounded-plate py-1 no-underline" href={`/jobs/${job.id}${router.search}`} data-job-link={job.id} aria-label={m.migrations_row_open_job({ status: look.label, percent })}>
		<span class="mb-1 flex items-center justify-between gap-2">
			<Plate tone={look.tone} icon={look.icon} spin={look.spin} label={look.label} />
			<span class="font-mono text-xs text-ink-2">{percent}%</span>
		</span>
		<StreamBars {job} />
	</a>
{:else}
	<span class="inline-flex items-center gap-1 text-ink-3"><Minus size={12} />{m.migrations_row_not_started()}</span>
{/if}
