<script lang="ts">
	/** What the last run of one mailbox found and did: damage by kind and folder, examples to preview, errors. */
	import Eye from '@lucide/svelte/icons/eye';
	import Wrench from '@lucide/svelte/icons/wrench';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { RepairBox, RepairExample, RepairIssue, RepairRun } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import { issueLabel } from './issues.ts';
	import RepairPreviewDialog from './RepairPreviewDialog.svelte';

	let { box, onrepair }: { box: RepairBox; onrepair: () => void } = $props();

	const runId = $derived(box.active_run?.id ?? box.last_scan?.id ?? null);
	const runStatus = $derived(box.active_run?.status ?? box.last_scan?.status ?? null);
	const seen = $derived(box.active_run?.progress.seen ?? 0);
	let run = $state.raw<RepairRun | null>(null);
	let error = $state<string | null>(null);
	let previewing = $state.raw<RepairExample | null>(null);

	// The full report is read when the run changes, ends, or (while it runs) every few hundred messages.
	const bucket = $derived(Math.floor(seen / 200));
	$effect(() => {
		void runStatus;
		void bucket;
		const id = runId;
		if (id == null) {
			run = null;
			return;
		}
		get<RepairRun>(`/repair/runs/${id}`).then(
			(r) => {
				if (id === runId) run = r;
			},
			(err) => (error = errorText(err))
		);
	});

	const report = $derived(run?.report);
	const issues = $derived(Object.entries(report?.issues ?? {}) as [RepairIssue, number][]);
	const folders = $derived(Object.entries(report?.folders ?? {}).filter(([, f]) => f.affected > 0 || f.repaired > 0));
	const canRepair = $derived(!box.active_run && run?.mode === 'scan' && run.status !== 'failed' && (run.progress.affected ?? 0) > 0);
</script>

{#if runId == null}
	<p class="m-0 text-ink-2">{m.repair_detail_never()}</p>
{:else if error && !run}
	<ErrorNotice title={m.repair_load_failed()} message={error} />
{:else if run && report}
	<div class="flex flex-col gap-3">
		<p class="m-0 text-sm text-ink-2">
			{run.mode === 'scan' ? m.repair_detail_scan() : run.mode === 'repair' ? m.repair_detail_repair() : m.repair_detail_restore()}
			{#if run.finished_at}<RelTime value={run.finished_at} />{/if}
		</p>
		{#if run.error}<ErrorNotice title={m.status_job_failed()} message={run.error} />{/if}
		{#if report.notes.includes('no_uidplus')}<Notice>{m.repair_note_no_uidplus()}</Notice>{/if}

		{#if issues.length}
			<ul class="m-0 flex list-none flex-col gap-1 p-0" data-testid="repair-issues">
				{#each issues as [issue, count] (issue)}
					<li class="flex items-baseline gap-2"><span class="w-14 shrink-0 text-right font-mono">{count}</span><span>{issueLabel(issue)}</span></li>
				{/each}
			</ul>
		{:else if run.mode === 'scan' && run.status !== 'failed'}
			<p class="m-0" data-testid="repair-issues-none">{m.repair_detail_nothing({ seen: run.progress.seen })}</p>
		{/if}

		{#if canRepair}
			<div><Button variant="primary" icon={Wrench} onclick={onrepair} data-testid="repair-detail-repair">{m.repair_action_repair()}</Button></div>
		{/if}

		{#if folders.length}
			<div class="overflow-x-auto">
				<table class="w-full text-sm" data-testid="repair-folders">
					<thead>
						<tr class="text-left text-ink-3">
							<th class="py-1 pr-3 font-medium">{m.repair_detail_folder()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.repair_detail_seen()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.repair_detail_damaged()}</th>
							<th class="py-1 text-right font-medium">{m.repair_detail_repaired()}</th>
						</tr>
					</thead>
					<tbody>
						{#each folders as [name, f] (name)}
							<tr class="border-t border-line">
								<td class="py-1 pr-3 font-mono text-xs">{prefs.text(name)}</td>
								<td class="py-1 pr-3 text-right font-mono">{f.seen}</td>
								<td class="py-1 pr-3 text-right font-mono">{f.affected}</td>
								<td class="py-1 text-right font-mono">{f.repaired}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if report.examples.length && run.mode === 'scan'}
			<div class="flex flex-col gap-1">
				<p class="m-0 text-sm font-medium">{m.repair_detail_examples()}</p>
				<ul class="m-0 flex list-none flex-col gap-1 p-0" data-testid="repair-examples">
					{#each report.examples.slice(0, 20) as ex (`${ex.folder}/${ex.uid}`)}
						<li class="flex flex-wrap items-center gap-2">
							<Button variant="ghost" icon={Eye} onclick={() => (previewing = ex)} data-testid="repair-preview">{m.repair_detail_preview()}</Button>
							<span class="min-w-0 flex-1 truncate">{prefs.text(ex.subject) || m.repair_no_subject()}</span>
							<span class="text-xs text-ink-3">{ex.issues.map(issueLabel).join(', ')}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/if}

		{#if report.errors.length}
			<details>
				<summary class="cursor-pointer text-sm">{m.repair_detail_errors({ count: run.progress.errors })}</summary>
				<ul class="m-0 mt-1 flex list-none flex-col gap-1 p-0 font-mono text-xs text-ink-2">
					{#each report.errors as err, i (i)}
						<li>{prefs.text(err.folder)}{err.uid ? ` #${err.uid}` : ''}: {prefs.text(err.msg)}</li>
					{/each}
				</ul>
			</details>
		{/if}
	</div>
{/if}

{#if previewing}
	<RepairPreviewDialog boxId={box.id} example={previewing} onclose={() => (previewing = null)} />
{/if}
