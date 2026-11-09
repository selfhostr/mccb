<script lang="ts">
	/** What the last run of one mailbox found and did: totals, folders and collections, examples, errors, log, history. */
	import CopyX from '@lucide/svelte/icons/copy-x';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { DedupBox, DedupRun } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { dedupLook } from '../../lib/status.ts';
	import Button from '../../lib/ui/Button.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import { KINDS, kindLabel, noteLabel, refusedLabel } from './helpers.ts';

	let { box, onremove }: { box: DedupBox; onremove: () => void } = $props();

	const runId = $derived(box.active_run?.id ?? box.last_run?.id ?? box.last_scan?.id ?? null);
	const runStatus = $derived(box.active_run?.status ?? box.last_run?.status ?? box.last_scan?.status ?? null);
	const seen = $derived(box.active_run?.progress.seen ?? 0);
	let run = $state.raw<DedupRun | null>(null);
	let history = $state.raw<DedupRun[]>([]);
	let error = $state<string | null>(null);

	// The full report is read when the run changes, ends, or (while it runs) every few hundred items.
	const bucket = $derived(Math.floor(seen / 200));
	$effect(() => {
		void runStatus;
		void bucket;
		const id = runId;
		if (id == null) {
			run = null;
			return;
		}
		get<DedupRun>(`/dedup/runs/${id}`).then(
			(r) => {
				if (id === runId) run = r;
			},
			(err) => (error = errorText(err))
		);
	});

	// The history changes when a run starts or ends.
	$effect(() => {
		void runId;
		void runStatus;
		const key = box.key;
		get<DedupRun[]>(`/dedup/boxes/${key}/runs?limit=20`).then(
			(r) => {
				if (key === box.key) history = r;
			},
			() => {}
		);
	});

	const report = $derived(run?.report);
	const totals = $derived(run ? KINDS.filter((k) => run!.totals[k]).map((k) => [k, run!.totals[k]!] as const) : []);
	const items = $derived(totals.reduce((n, [, t]) => n + t.items, 0));
	const folders = $derived((report?.folders ?? []).filter((f) => f.duplicates > 0 || f.deleted > 0 || f.refused));
	const removal = $derived(run?.mode === 'dedup');
	const canRemove = $derived(!box.active_run && !box.busy && box.usable && !!box.last_scan && box.last_scan.progress.duplicates > 0);

	const histCount = (r: DedupRun) => (r.mode === 'dedup' ? m.dedup_hist_removed({ count: fmtNum(r.progress.deleted) }) : m.dedup_hist_found({ count: fmtNum(r.progress.duplicates) }));
	const modeLabel = (r: DedupRun) => (r.mode === 'scan' ? m.dedup_mode_scan() : m.dedup_mode_dedup());
</script>

{#if runId == null}
	<p class="m-0 text-ink-2">{m.dedup_detail_never()}</p>
{:else if error && !run}
	<ErrorNotice title={m.dedup_load_failed()} message={error} />
{:else if run && report}
	<div class="flex flex-col gap-3">
		<p class="m-0 text-sm text-ink-2">
			{removal ? m.dedup_detail_dedup() : m.dedup_detail_scan()}
			{#if run.finished_at}<RelTime value={run.finished_at} />{/if}
			· {run.kinds.map(kindLabel).join(', ')}
		</p>
		{#if run.error}<ErrorNotice title={m.status_job_failed()} message={run.error} />{/if}
		{#each report.notes as note (note)}<Notice>{noteLabel(note)}</Notice>{/each}

		{#if totals.length}
			<div class="overflow-x-auto">
				<table class="w-full text-sm" data-testid="dedup-totals">
					<thead>
						<tr class="text-left text-ink-3">
							<th class="py-1 pr-3 font-medium">{m.dedup_col_type()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_items()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_groups()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_duplicates()}</th>
							{#if removal}<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_deleted()}</th>{/if}
							<th class="py-1 text-right font-medium">{m.dedup_col_skipped()}</th>
						</tr>
					</thead>
					<tbody>
						{#each totals as [kind, t] (kind)}
							<tr class="border-t border-line" data-kind={kind}>
								<td class="py-1 pr-3">{kindLabel(kind)}</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(t.items)}</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(t.groups)}</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(t.duplicates)}</td>
								{#if removal}<td class="py-1 pr-3 text-right font-mono">{fmtNum(t.deleted)}</td>{/if}
								<td class="py-1 text-right font-mono">{fmtNum(t.skipped)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if !folders.length && run.status !== 'failed' && run.progress.duplicates === 0}
			<p class="m-0" data-testid="dedup-none">{m.dedup_detail_nothing({ items: fmtNum(items) })}</p>
		{/if}

		{#if canRemove}
			<div><Button variant="primary" icon={CopyX} onclick={onremove} data-testid="dedup-detail-remove">{m.dedup_action_remove()}</Button></div>
		{/if}

		{#if folders.length}
			<div class="overflow-x-auto">
				<table class="w-full text-sm" data-testid="dedup-folders">
					<thead>
						<tr class="text-left text-ink-3">
							<th class="py-1 pr-3 font-medium">{m.dedup_col_type()}</th>
							<th class="py-1 pr-3 font-medium">{m.dedup_col_folder()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_items()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_groups()}</th>
							<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_duplicates()}</th>
							{#if removal}<th class="py-1 pr-3 text-right font-medium">{m.dedup_col_deleted()}</th>{/if}
							<th class="py-1 text-right font-medium">{m.dedup_col_skipped()}</th>
						</tr>
					</thead>
					<tbody>
						{#each folders as f (`${f.kind}/${f.name}`)}
							<tr class="border-t border-line align-top">
								<td class="py-1 pr-3">{kindLabel(f.kind)}</td>
								<td class="py-1 pr-3 font-mono text-xs">
									{prefs.text(f.name)}
									{#if f.refused}<span class="block font-sans text-warning-ink" data-testid="dedup-refused">{refusedLabel(f.refused)}</span>{/if}
								</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(f.items)}</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(f.groups)}</td>
								<td class="py-1 pr-3 text-right font-mono">{fmtNum(f.duplicates)}</td>
								{#if removal}<td class="py-1 pr-3 text-right font-mono">{fmtNum(f.deleted)}</td>{/if}
								<td class="py-1 text-right font-mono">{fmtNum(f.skipped)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if report.examples.length}
			<div class="flex flex-col gap-1">
				<p class="m-0 text-sm font-medium">{m.dedup_detail_examples()}</p>
				<ul class="m-0 flex list-none flex-col gap-2 p-0" data-testid="dedup-examples">
					{#each report.examples.slice(0, 20) as ex, i (i)}
						<li class="flex min-w-0 flex-col">
							<span class="truncate">{prefs.text(ex.label)}</span>
							<span class="truncate text-xs text-ink-2">{kindLabel(ex.kind)} · {prefs.text(ex.folder)} · {prefs.text(ex.detail)}</span>
							<span class="break-all text-xs text-ink-3">{m.dedup_example_copies({ count: ex.copies, keep: ex.keep })}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/if}

		{#if report.errors.length}
			<details>
				<summary class="cursor-pointer text-sm">{m.dedup_detail_errors({ count: report.errors.length })}</summary>
				<ul class="m-0 mt-1 flex list-none flex-col gap-1 p-0 font-mono text-xs break-words text-ink-2">
					{#each report.errors as err, i (i)}
						<li>{prefs.text(err.folder)}: {prefs.text(err.msg)}</li>
					{/each}
				</ul>
			</details>
		{/if}

		{#if report.log.length}
			<details>
				<summary class="cursor-pointer text-sm">{m.dedup_detail_log()}</summary>
				<ul class="m-0 mt-1 flex list-none flex-col gap-1 p-0 font-mono text-xs break-words text-ink-2" data-testid="dedup-log">
					{#each report.log as line, i (i)}
						<li class={line.level === 'error' ? 'text-warning-ink' : ''}><RelTime value={line.ts} /> {line.level}: {prefs.text(line.msg)}</li>
					{/each}
				</ul>
			</details>
		{/if}
	</div>
{/if}

{#if history.length}
	<div class="mt-3 flex flex-col gap-1">
		<p class="m-0 text-sm font-medium">{m.dedup_detail_history()}</p>
		<div class="overflow-x-auto">
			<table class="w-full text-sm" data-testid="dedup-history">
				<tbody>
					{#each history as h (h.id)}
						<tr class="border-t border-line">
							<td class="py-1 pr-3">{modeLabel(h)}</td>
							<td class="py-1 pr-3">{dedupLook({ active_run: null, last_run: h, last_scan: null }).label}</td>
							<td class="py-1 pr-3 text-ink-2"><RelTime value={h.finished_at ?? h.created_at} /></td>
							<td class="py-1 pr-3 text-right font-mono">{histCount(h)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</div>
{/if}
