<script lang="ts">
	/**
	 * Says what will start and what will not, before anything happens. A dry run can be asked for first.
	 * For mailboxes that already ran, the run can also mirror: one job per mailbox copies what is new at the source,
	 * then deletes and moves at the destination what was deleted or moved there, within the safety limit (checked by
	 * each job, which deletes nothing past it). A comparison that writes nothing can be opened first.
	 */
	import GitCompareArrows from '@lucide/svelte/icons/git-compare-arrows';
	import ListFilter from '@lucide/svelte/icons/list-filter';
	import Play from '@lucide/svelte/icons/play';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import { onDestroy, untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { getLocale } from '#lib/paraglide/runtime.js';
	import { errorText, get, post } from '../../lib/api/client.ts';
	import type { Account, Job, MirrorRun, Plan, RunInput } from '../../lib/api/types.ts';
	import type { ImportPlan } from '../../lib/logic/accounts.ts';
	import { isActive } from '../../lib/logic/accounts.ts';
	import { clampCount, clampPercent, DEFAULT_MAX_DELETE_PERCENT, DEFAULT_MIN_DELETE_COUNT, MAX_MIN_DELETE_COUNT, startedJobs } from '../../lib/logic/mirror.ts';
	import { planDone } from '../../lib/logic/plan.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import NoDavNotice from './NoDavNotice.svelte';
	import PlanResult from './PlanResult.svelte';

	interface Props {
		plan: ImportPlan;
		onclose: () => void;
		ondone: (ids: number[]) => void;
		/** Opens the mirror comparison (dry run) of these mailboxes. */
		onpreview?: (ids: number[]) => void;
	}

	const SHOWN = 5;

	let { plan, onclose, ondone, onpreview }: Props = $props();
	let busy = $state(false);
	let error = $state<string | null>(null);
	/** Several mailboxes at once make a batch by default, named after the day: one mailbox does not. */
	let name = $state(untrack(() => (plan.ready.length + plan.refused.length > 1 ? m.migrations_import_batch_default({ date: new Date().toLocaleDateString(getLocale(), { day: '2-digit', month: '2-digit' }) }) : '')));
	let mode = $state<'full' | 'delta'>('full');
	let includeRefused = $state(false);
	let mirror = $state(false);
	let percentText = $state(String(DEFAULT_MAX_DELETE_PERCENT));
	let minCountText = $state(String(DEFAULT_MIN_DELETE_COUNT));
	/** The selection was cut down to the ready mailboxes: the others are no longer listed. */
	let trimmed = $state(false);
	let dry = $state.raw<Plan | null>(null);
	let dryBusy = $state(false);
	let dryError = $state<string | null>(null);
	let gone = false;
	onDestroy(() => (gone = true));

	const starting = $derived(includeRefused && !trimmed ? [...plan.ready, ...plan.refused] : plan.ready);
	const ids = $derived(starting.map((a) => a.id));
	/** Mailboxes that already ran: only then is a choice between a full run and a final resync worth asking. */
	const hasRun = $derived(starting.some((a) => {
		const job = data.jobs.get(a.id);
		return !!job && !isActive(job);
	}));

	const problems = $derived(
		trimmed
			? []
			: [
					{ key: 'pst', text: m.migrations_import_no_pst({ count: plan.noPst.length }), list: plan.noPst },
					{ key: 'password', text: m.migrations_import_no_password({ count: plan.noPassword.length }), list: plan.noPassword },
					{ key: 'refused', text: m.migrations_import_refused({ count: plan.refused.length }), list: plan.refused },
					{ key: 'active', text: m.migrations_import_active({ count: plan.active.length }), list: plan.active }
				].filter((p) => p.list.length)
	);
	/** Informational: those that start anyway, grouped by the server whose DAV is off; gone once it is turned on. */
	const noDav = $derived(
		data.servers
			.filter((server) => !server.dav_enabled)
			.map((server) => ({ server, accounts: plan.noDav.filter((a) => a.server_id === server.id && ids.includes(a.id)) }))
			.filter((g) => g.accounts.length)
	);
	/** Only a selection can be cut down: a dialog opened from one mailbox's panel has none to change. */
	const canTrim = $derived(problems.some((p) => p.list.some((a) => data.selected.has(a.id))));

	function keepReady() {
		for (const problem of problems) for (const a of problem.list) data.selected.delete(a.id);
		includeRefused = false;
		trimmed = true;
	}

	const listed = (list: Account[]) => list.slice(0, SHOWN).map((a) => prefs.email(a.email)).join(', ');

	async function dryRun() {
		dryBusy = true;
		dryError = null;
		dry = null;
		try {
			let current = await post<Plan>('/runs/plan', { account_ids: ids });
			while (!planDone(current)) {
				dry = current;
				await new Promise((resolve) => setTimeout(resolve, 1000));
				if (gone) return;
				current = await get<Plan>(`/plans/${current.id}`);
			}
			dry = current;
			if (current.status === 'failed') dryError = current.error || m.plan_failed();
		} catch (e) {
			dryError = errorText(e);
		} finally {
			dryBusy = false;
		}
	}

	/** A mirror run: the switch of each mailbox is turned on first (it is what the server checks), then one job each. */
	async function startMirror(body: RunInput) {
		const off = ids.filter((id) => data.accounts.get(id) && !data.accounts.get(id)?.options?.mirror);
		await Promise.all(off.map((id) => data.setOption(id, 'mirror', true)));
		const percent = clampPercent(percentText);
		const minCount = clampCount(minCountText);
		percentText = String(percent);
		minCountText = String(minCount);
		const answer = await post<MirrorRun>('/runs', { ...body, mode: 'mirror', dry_run: false, max_delete_percent: percent, min_delete_count: minCount, allow_large_delete: false, allow_empty_folders: [] });
		const jobs = startedJobs(answer.jobs ?? []);
		for (const job of jobs) data.upsertJob(job);
		const started = new Set(jobs.map((job) => job.account_id));
		const left = (answer.accounts ?? []).filter((a) => !started.has(a.account_id));
		if (!jobs.length) {
			error = left.map((a) => `${prefs.email(a.email || `#${a.account_id}`)}: ${(a.blocked ?? []).map((b) => prefs.text(b.message)).join('; ')}`).join(' · ') || m.migrations_mirror_apply_failed();
			return;
		}
		if (body.name) void data.loadBatches();
		toast(m.migrations_mirror_queued({ count: jobs.length }), 'signal');
		if (left.length) toast(m.migrations_mirror_start_skipped({ count: left.length }), 'warning');
		ondone(jobs.map((job) => job.account_id));
	}

	async function start() {
		busy = true;
		error = null;
		if (hasRun && mirror) {
			const body: RunInput = { account_ids: ids };
			if (name.trim()) body.name = name.trim();
			try {
				await startMirror(body);
			} catch (e) {
				error = errorText(e);
			} finally {
				busy = false;
			}
			return;
		}
		const body: RunInput = { account_ids: ids };
		if (name.trim()) body.name = name.trim();
		if (hasRun && mode === 'delta') body.mode = 'delta';
		try {
			const jobs = await post<Job[]>('/runs', body);
			for (const job of jobs) data.upsertJob(job);
			if (body.name) void data.loadBatches();
			toast(m.migrations_import_queued({ count: jobs.length }), 'signal');
			ondone(ids);
		} catch (e) {
			error = errorText(e);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.migrations_import_title()} {onclose} wide>
	<div class="flex flex-col gap-3">
		<p class="m-0">{ids.length ? m.migrations_import_ready({ count: ids.length }) : m.migrations_import_none()}</p>
		{#each problems as problem (problem.key)}
			<Notice>
				<p class="m-0">{problem.text}</p>
				<p class="m-0 mt-1 font-mono text-xs" data-testid={`import-${problem.key}-list`}>
					{listed(problem.list)}{#if problem.list.length > SHOWN}<span class="font-sans">{' '}{m.migrations_import_more({ count: problem.list.length - SHOWN })}</span>{/if}
				</p>
				{#if problem.key === 'refused'}
					<label class="mt-1 flex min-h-10 items-center gap-2">
						<span class="check"><input type="checkbox" bind:checked={includeRefused} data-testid="import-refused-include" /></span>
						{m.migrations_import_refused_include()}
					</label>
				{/if}
			</Notice>
		{/each}
		{#each noDav as group (group.server.id)}
			<NoDavNotice server={group.server} accounts={group.accounts} />
		{/each}
		{#if canTrim && plan.ready.length}
			<div><Button icon={ListFilter} onclick={keepReady} data-testid="import-keep-ready">{m.migrations_import_keep_ready()}</Button></div>
		{/if}
		{#if ids.length}
			<Field label={m.migrations_import_batch()} hint={m.migrations_import_batch_hint()}>
				<input class="input" maxlength="100" autocomplete="off" bind:value={name} data-testid="import-batch" />
			</Field>
			{#if hasRun}
				<Field label={m.migrations_import_mode()} hint={m.migrations_import_mode_hint()}>
					<select class="input" bind:value={mode} disabled={mirror} data-testid="import-mode">
						<option value="full">{m.migrations_import_mode_full()}</option>
						<option value="delta">{m.migrations_import_mode_delta()}</option>
					</select>
				</Field>
				<div class="flex flex-col gap-2" data-testid="import-mirror">
					<label class="flex min-h-10 items-center gap-2">
						<span class="check"><input type="checkbox" bind:checked={mirror} data-testid="import-mirror-toggle" /></span>
						<span class="font-medium">{m.migrations_import_mirror()}</span>
					</label>
					<p class="m-0 pl-7 text-xs text-ink-2">{m.migrations_import_mirror_hint()}</p>
					{#if mirror}
						<div class="flex flex-col gap-3 border-l-2 border-line pl-5 ml-2" data-testid="import-mirror-options">
							<Field label={m.migrations_mirror_limit_label()} hint={m.migrations_mirror_limit_hint()}>
								<input class="input w-24 font-mono" type="number" min="1" max="100" step="1" inputmode="numeric" bind:value={percentText} onchange={() => (percentText = String(clampPercent(percentText)))} data-testid="import-mirror-percent" />
							</Field>
							<Field label={m.migrations_mirror_min_count_label()} hint={m.migrations_mirror_min_count_hint()}>
								<input class="input w-24 font-mono" type="number" min="0" max={MAX_MIN_DELETE_COUNT} step="1" inputmode="numeric" bind:value={minCountText} onchange={() => (minCountText = String(clampCount(minCountText)))} data-testid="import-mirror-min-count" />
							</Field>
							<p class="m-0 text-xs text-ink-2">{m.migrations_import_mirror_safety()}</p>
							{#if onpreview}
								<div><Button variant="ghost" icon={GitCompareArrows} onclick={() => onpreview(ids)} data-testid="import-mirror-preview">{m.migrations_import_mirror_preview()}</Button></div>
							{/if}
						</div>
					{/if}
				</div>
			{/if}
		{/if}
		{#if dryError}<ErrorNotice title={m.plan_failed()} message={dryError} onretry={dryRun} />{/if}
		{#if dryBusy}
			<p class="m-0 text-ink-2" role="status" data-testid="plan-running">
				{dry?.result ? m.plan_progress({ done: dry.result.done, total: dry.result.total }) : m.plan_running()}
			</p>
		{/if}
		{#if dry?.result?.accounts.length && !dryBusy}<PlanResult result={dry.result} />{/if}
		{#if error}<ErrorNotice title={m.migrations_import_failed()} message={error} onretry={start} />{/if}
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button icon={ScanSearch} loading={dryBusy} disabled={!ids.length} onclick={dryRun} data-testid="import-dry">{m.plan_dry_run()}</Button>
		<Button variant="primary" icon={hasRun && mirror ? GitCompareArrows : Play} loading={busy} disabled={!ids.length} onclick={start} data-testid="import-confirm">{hasRun && mirror ? m.migrations_import_start_mirror() : m.migrations_import_start()}</Button>
	{/snippet}
</Dialog>
