<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import FileClock from '@lucide/svelte/icons/file-clock';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, get } from '../../lib/api/client.ts';
	import type { JobStatus } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import Button from '../../lib/ui/Button.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';

	type Section = Record<string, number | undefined>;
	type Report = Record<string, unknown> & { folders?: { path: string[] | string; name?: string; sent?: number; present?: number }[]; sample_check?: unknown };

	let { jobId, status }: { jobId: number; status: JobStatus } = $props();

	let report = $state.raw<Report | null>(null);
	let missing = $state(false);
	let error = $state<string | null>(null);

	async function load() {
		error = null;
		missing = false;
		try {
			report = await get<Report>(`/jobs/${jobId}/report`);
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) missing = true;
			else error = errorText(e);
		}
	}

	$effect(() => {
		void status;
		void load();
	});

	const sections = $derived.by(() => {
		if (!report) return [];
		const source = report as Record<string, Record<string, Section> | Section>;
		const totals = (source.totals ?? source.summary ?? {}) as Record<string, Section>;
		return [
			{ key: 'mail', label: m.migrations_row_content_mail() },
			{ key: 'calendar', label: m.migrations_row_content_calendar() },
			{ key: 'contacts', label: m.migrations_row_content_contacts() }
		]
			.map((s) => ({ ...s, data: (source[s.key] ?? totals[s.key]) as Section | undefined }))
			.filter((s) => s.data && typeof s.data === 'object');
	});
</script>

{#if error}
	<ErrorNotice title={m.jobs_report_failed()} message={error} onretry={load} />
{:else if missing}
	<EmptyState icon={FileClock} title={m.jobs_report_missing()} description={m.jobs_report_missing_text()} />
{:else if !report}
	<Skeleton class="h-32" />
{:else}
	<div class="flex flex-col gap-3" data-testid="job-report">
		<Button icon={Download} href={`/api/jobs/${jobId}/report?download=1`} download={`job-report-${jobId}.json`}>{m.jobs_report_download()}</Button>
		{#if sections.length}
			<table class="w-full text-xs">
				<thead>
					<tr class="text-left text-ink-2">
						<th class="py-1 font-medium">{m.jobs_report_type()}</th>
						<th class="py-1 text-right font-medium">{m.jobs_report_sent()}</th>
						<th class="py-1 text-right font-medium">{m.jobs_report_present()}</th>
						<th class="py-1 text-right font-medium">{m.jobs_report_errors()}</th>
					</tr>
				</thead>
				<tbody class="font-mono">
					{#each sections as section (section.key)}
						{@const sent = section.data?.sent ?? section.data?.ok ?? section.data?.done}
						{@const present = section.data?.present ?? section.data?.server}
						<tr class="border-t border-line">
							<td class="py-1 font-sans">{section.label}</td>
							<td class="py-1 text-right">{fmtNum(sent)}</td>
							<td class={`py-1 text-right ${present != null && sent != null && present < sent ? 'text-warning-ink' : ''}`}>{fmtNum(present)}</td>
							<td class="py-1 text-right">{fmtNum(section.data?.errors ?? 0)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<pre class="m-0 max-h-80 overflow-auto rounded-btn border border-line bg-canvas-deep p-2 font-mono text-xs whitespace-pre-wrap">{JSON.stringify(report, null, 2)}</pre>
		{/if}
		{#if report.sample_check}
			<p class="m-0 text-ink-2">{m.jobs_report_sample({ result: typeof report.sample_check === 'string' ? report.sample_check : JSON.stringify(report.sample_check) })}</p>
		{/if}
	</div>
{/if}
