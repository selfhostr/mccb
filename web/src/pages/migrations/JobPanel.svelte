<script lang="ts">
	import Ban from '@lucide/svelte/icons/ban';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import X from '@lucide/svelte/icons/x';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get, post } from '../../lib/api/client.ts';
	import type { Job, JobPhase } from '../../lib/api/types.ts';
	import { fmtBytes, fmtDuration, fmtNum, pct } from '../../lib/format.ts';
	import { isActive } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { jobLookOf } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import Tabs from '../../lib/ui/Tabs.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import JobErrors from './JobErrors.svelte';
	import JobLog from './JobLog.svelte';
	import JobReport from './JobReport.svelte';

	let { id, onclose }: { id: number; onclose: () => void } = $props();

	let fetched = $state.raw<Job | null>(null);
	let loadError = $state<string | null>(null);
	let confirmStop = $state(false);
	let busy = $state<string | null>(null);
	let heading = $state<HTMLElement>();

	/** The map holds the latest job of an account: live when it is this one, else what was fetched. */
	const job = $derived.by(() => {
		if (!fetched) return null;
		const live = data.jobs.get(fetched.account_id);
		return live && live.id === id ? live : fetched;
	});
	const account = $derived(job ? data.accounts.get(job.account_id) : undefined);
	const look = $derived(job ? jobLookOf(job) : null);
	const tab = $derived.by(() => {
		const value = router.params.get('tab');
		return value === 'errors' || value === 'report' ? value : 'log';
	});

	const ORDER: JobPhase[] = ['precheck', 'mail', 'calendar', 'contacts', 'verify'];
	const PHASE_LABEL = $derived<Record<JobPhase, string>>({
		precheck: m.jobs_phase_precheck(),
		mail: m.migrations_row_content_mail(),
		calendar: m.migrations_row_content_calendar(),
		contacts: m.migrations_row_content_contacts(),
		verify: m.jobs_phase_verify()
	});
	const streams = $derived(
		job
			? [
					...(['mail', 'calendar', 'contacts'] as const).map((key) => ({ key, label: PHASE_LABEL[key], ...job.progress[key] })),
					// Only on routes that carry tasks.
					...(job.progress.tasks ? [{ key: 'tasks', label: m.jobs_stream_tasks(), ...job.progress.tasks }] : [])
				].filter((s) => s.total || s.done)
			: []
	);
	const errorCount = $derived(streams.reduce((sum, s) => sum + (s.errors ?? 0), 0));

	async function load() {
		loadError = null;
		try {
			fetched = await get<Job>(`/jobs/${id}`);
		} catch (error) {
			loadError = errorText(error);
		}
	}

	async function act(action: string, message: string) {
		busy = action;
		try {
			const updated = await post<Job>(`/jobs/${id}/${action}`);
			fetched = updated;
			data.upsertJob(updated);
			toast(message, 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		} finally {
			busy = null;
		}
	}

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && !document.querySelector('dialog[open]')) onclose();
	}

	onMount(() => {
		void load().then(() => heading?.focus());
	});
</script>

<svelte:window {onkeydown} />

<div class="flex items-start justify-between gap-2 border-b border-line py-2 pr-2 pl-4">
	<div class="min-w-0 pt-1">
		<h2 bind:this={heading} tabindex="-1" class="truncate font-mono text-sm outline-none" data-testid="job-title">
			{account ? prefs.email(account.email) : m.jobs_panel_title({ id })}
		</h2>
		{#if job && look}
			<p class="m-0 mt-1 flex flex-wrap items-center gap-2 text-ink-2">
				<Plate tone={look.tone} icon={look.icon} spin={look.spin} label={look.label} />
				{#if job.phase && isActive(job)}<span>{PHASE_LABEL[job.phase] ?? job.phase}</span>{/if}
			</p>
		{/if}
	</div>
	<IconButton icon={X} label={m.jobs_panel_close()} onclick={onclose} data-testid="job-close" />
</div>

<div class="min-h-0 flex-1 overflow-y-auto">
	{#if loadError}
		<div class="p-4"><ErrorNotice title={m.jobs_panel_load_failed({ id })} message={loadError} onretry={load} /></div>
	{:else if !job}
		<div class="flex flex-col gap-3 p-4"><Skeleton class="h-10" /><Skeleton class="h-24" /><Skeleton class="h-64" /></div>
	{:else}
		<div class="flex flex-col gap-3 p-4">
			<div class="flex flex-wrap gap-2">
				{#if job.status === 'running' || job.status === 'queued'}
					<Button icon={Pause} loading={busy === 'pause'} onclick={() => act('pause', m.jobs_action_pause_done())}>{m.jobs_action_pause()}</Button>
				{/if}
				{#if job.status === 'paused'}
					<Button variant="primary" icon={Play} loading={busy === 'resume'} onclick={() => act('resume', m.jobs_action_resume_done())}>{m.jobs_action_resume()}</Button>
				{/if}
				{#if job.status === 'running' || job.status === 'queued' || job.status === 'paused'}
					<Button variant="danger" icon={Ban} onclick={() => (confirmStop = true)}>{m.jobs_action_stop()}</Button>
				{/if}
				{#if !isActive(job)}
					<Button variant="primary" icon={RotateCw} loading={busy === 'retry'} onclick={() => act('retry', m.jobs_action_retry_done())}>{m.jobs_action_retry()}</Button>
				{/if}
				{#if job.status === 'done' || job.status === 'done_with_errors'}
					<Button icon={RefreshCw} loading={busy === 'delta'} onclick={() => act('delta', m.jobs_action_delta_done())} data-testid="job-delta">{m.jobs_action_delta()}</Button>
				{/if}
				{#if job.status === 'done_with_errors' || job.status === 'stopped' || job.status === 'failed'}
					<Button icon={RotateCw} loading={busy === 'retry-errors'} onclick={() => act('retry-errors', m.jobs_action_retry_errors_done())}>{m.jobs_action_retry_errors()}</Button>
				{/if}
			</div>

			{#if job.error}<ErrorNotice title={m.jobs_panel_failed()} message={prefs.text(job.error)} />{/if}

			<ol class="m-0 flex list-none flex-wrap gap-x-3 gap-y-1 p-0 text-xs" aria-label={m.jobs_phase_label()}>
				{#each ORDER as phase, i (phase)}
					{@const current = ORDER.indexOf(job.phase ?? 'precheck')}
					{@const finished = job.status === 'done' || job.status === 'done_with_errors' || i < current}
					<li class={finished ? 'text-ink-2' : i === current ? 'font-semibold text-ink' : 'text-ink-3'} aria-current={i === current ? 'step' : undefined}>
						{PHASE_LABEL[phase]}
					</li>
				{/each}
			</ol>

			<div class="flex flex-col gap-2">
				{#each streams as stream (stream.key)}
					<div>
						<div class="mb-1 flex justify-between gap-2">
							<span class="font-medium">{stream.label}</span>
							<span class="font-mono text-xs text-ink-2">
								{m.jobs_stream_count({ done: fmtNum(stream.done), total: fmtNum(stream.total) })}
								{#if stream.errors}<span class="text-advisory-ink">{m.jobs_stream_errors({ count: fmtNum(stream.errors) })}</span>{/if}
							</span>
						</div>
						<Progress value={pct(stream.done, stream.total)} tone={stream.errors ? 'advisory' : job.status === 'done' ? 'signal' : 'ink'} label={stream.label} height="h-1.5" />
					</div>
				{/each}
			</div>

			<dl class="m-0 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
				<div><dt class="text-ink-3">{job.status === 'running' ? m.jobs_stat_rate() : m.jobs_stat_average()}</dt><dd class="m-0 font-mono">{job.rate != null ? m.jobs_stat_rate_value({ rate: fmtNum(Math.round(job.rate * 10) / 10) }) : '-'}{#if job.rate_bytes}<span class="block">{fmtBytes(job.rate_bytes)}/s</span>{/if}</dd></div>
				<div><dt class="text-ink-3">{m.jobs_stat_left()}</dt><dd class="m-0 font-mono">{job.status === 'running' ? fmtDuration(job.eta) : '-'}</dd></div>
				<div><dt class="text-ink-3">{m.jobs_stat_started()}</dt><dd class="m-0"><RelTime value={job.started_at} /></dd></div>
				<div><dt class="text-ink-3">{m.jobs_stat_finished()}</dt><dd class="m-0"><RelTime value={job.finished_at} /></dd></div>
				{#if job.tuning}
					<div class="col-span-2"><dt class="text-ink-3">{m.jobs_stat_tuning()}</dt><dd class="m-0">{m.jobs_stat_tuning_value({ builders: job.tuning.builders, connections: job.tuning.connections })}</dd></div>
				{/if}
				{#if job.current_folder}
					<div class="col-span-full"><dt class="text-ink-3">{m.jobs_stat_folder()}</dt><dd class="m-0 font-mono break-words">{job.current_folder}</dd></div>
				{/if}
			</dl>

			<Tabs
				label={m.jobs_tabs_label()}
				value={tab}
				tabs={[
					{ id: 'log', label: m.jobs_tab_log() },
					{ id: 'errors', label: errorCount ? m.jobs_tab_errors_count({ count: errorCount }) : m.jobs_tab_errors() },
					{ id: 'report', label: m.jobs_tab_report() }
				]}
				onchange={(next) => router.setParams({ tab: next === 'log' ? null : next })}
			/>
			{#if tab === 'log'}
				<JobLog jobId={id} active={isActive(job)} />
			{:else if tab === 'errors'}
				<JobErrors jobId={id} refreshKey={errorCount} />
			{:else}
				<JobReport jobId={id} status={job.status} />
			{/if}
		</div>
	{/if}
</div>

{#if confirmStop}
	<Confirm
		title={m.jobs_stop_title()}
		text={m.jobs_stop_text()}
		confirm={m.jobs_action_stop()}
		danger
		onconfirm={() => act('stop', m.jobs_action_stop_done())}
		onclose={() => (confirmStop = false)}
	/>
{/if}
