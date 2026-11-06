<script lang="ts">
	/** Says what will start and what will not, before anything happens. A dry run can be asked for first. */
	import Play from '@lucide/svelte/icons/play';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import { onDestroy } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get, post } from '../../lib/api/client.ts';
	import type { Job, Plan, RunInput } from '../../lib/api/types.ts';
	import type { ImportPlan } from '../../lib/logic/accounts.ts';
	import { isActive } from '../../lib/logic/accounts.ts';
	import { planDone } from '../../lib/logic/plan.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import PlanResult from './PlanResult.svelte';

	interface Props {
		plan: ImportPlan;
		onclose: () => void;
		ondone: (ids: number[]) => void;
	}

	let { plan, onclose, ondone }: Props = $props();
	let busy = $state(false);
	let error = $state<string | null>(null);
	let name = $state('');
	let mode = $state<'full' | 'delta'>('full');
	let dry = $state.raw<Plan | null>(null);
	let dryBusy = $state(false);
	let dryError = $state<string | null>(null);
	let gone = false;
	onDestroy(() => (gone = true));

	const ids = $derived(plan.ready.map((a) => a.id));
	/** Mailboxes that already ran: only then is a choice between a full run and a final resync worth asking. */
	const hasRun = $derived(plan.ready.some((a) => {
		const job = data.jobs.get(a.id);
		return !!job && !isActive(job);
	}));

	const problems = $derived([
		{ key: 'pst', text: m.migrations_import_no_pst({ count: plan.noPst.length }), list: plan.noPst },
		{ key: 'password', text: m.migrations_import_no_password({ count: plan.noPassword.length }), list: plan.noPassword },
		{ key: 'active', text: m.migrations_import_active({ count: plan.active.length }), list: plan.active }
	].filter((p) => p.list.length));

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

	async function start() {
		busy = true;
		error = null;
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
		<p class="m-0">{plan.ready.length ? m.migrations_import_ready({ count: plan.ready.length }) : m.migrations_import_none()}</p>
		{#each problems as problem (problem.key)}
			<Notice>
				<p class="m-0">{problem.text}</p>
				<p class="m-0 mt-1 font-mono text-xs">{problem.list.slice(0, 5).map((a) => prefs.email(a.email)).join(', ')}</p>
			</Notice>
		{/each}
		{#if plan.ready.length}
			<Disclosure title={m.migrations_import_options()} data-testid="import-options">
				<div class="flex flex-col gap-3">
					<Field label={m.migrations_import_batch()} hint={m.migrations_import_batch_hint()}>
						<input class="input" maxlength="100" autocomplete="off" bind:value={name} data-testid="import-batch" />
					</Field>
					{#if hasRun}
						<Field label={m.migrations_import_mode()} hint={m.migrations_import_mode_hint()}>
							<select class="input" bind:value={mode} data-testid="import-mode">
								<option value="full">{m.migrations_import_mode_full()}</option>
								<option value="delta">{m.migrations_import_mode_delta()}</option>
							</select>
						</Field>
					{/if}
				</div>
			</Disclosure>
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
		<Button icon={ScanSearch} loading={dryBusy} disabled={!plan.ready.length} onclick={dryRun} data-testid="import-dry">{m.plan_dry_run()}</Button>
		<Button variant="primary" icon={Play} loading={busy} disabled={!plan.ready.length} onclick={start} data-testid="import-confirm">{m.migrations_import_start()}</Button>
	{/snippet}
</Dialog>
