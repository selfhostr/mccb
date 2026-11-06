<script lang="ts">
	/**
	 * The batch picked in the filter row: how far it is and what can be done to all of its jobs at once.
	 * Only the actions that would change something are offered.
	 */
	import Ban from '@lucide/svelte/icons/ban';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get, post } from '../../lib/api/client.ts';
	import type { Batch, BatchAction } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import { batchActions, batchItems, batchLookStatus } from '../../lib/logic/batch.ts';
	import { jobLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	let { id }: { id: number } = $props();

	const batch = $derived(data.batches.find((b) => b.id === id));
	const look = $derived(batch ? jobLook(batchLookStatus(batch)) : null);
	const items = $derived(batch ? batchItems(batch) : null);
	const actions = $derived(batch ? batchActions(batch) : []);
	let busy = $state<BatchAction | null>(null);
	let confirmStop = $state(false);

	const LABEL = $derived<Record<BatchAction, string>>({
		pause: m.jobs_action_pause(),
		resume: m.jobs_action_resume(),
		stop: m.jobs_action_stop(),
		'retry-failed': m.batch_retry_failed()
	});
	const ICON = { pause: Pause, resume: Play, stop: Ban, 'retry-failed': RotateCw };

	async function refresh() {
		try {
			data.upsertBatch(await get<Batch>(`/batches/${id}`));
		} catch {
			// The next round tries again.
		}
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => {
			if (batch?.state !== 'finished') void refresh();
		}, 3000);
		return () => clearInterval(timer);
	});

	async function run(action: BatchAction) {
		busy = action;
		try {
			const result = await post<Batch>(`/batches/${id}/${action}`);
			data.upsertBatch(result);
			toast(m.batch_done({ changed: result.changed ?? 0, skipped: result.skipped ?? 0 }), 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		} finally {
			busy = null;
		}
	}
</script>

{#if batch && look && items}
	<div class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-btn border border-line-strong px-3 py-1" role="region" aria-label={m.batch_label({ name: batch.name })} data-testid="batch-bar">
		<div class="min-w-0 flex-1 basis-56">
			<p class="m-0 flex flex-wrap items-center gap-2 font-medium">
				<span class="truncate">{batch.name}</span>
				<Plate tone={look.tone} icon={look.icon} label={look.label} />
			</p>
			<p class="m-0 text-xs text-ink-2" data-testid="batch-totals">{m.batch_totals({ jobs: batch.jobs, done: fmtNum(items.done), total: fmtNum(items.total), errors: fmtNum(items.errors) })}</p>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			{#each actions as action (action)}
				<Button
					variant={action === 'stop' ? 'danger' : 'secondary'}
					icon={ICON[action]}
					loading={busy === action}
					onclick={() => (action === 'stop' ? (confirmStop = true) : run(action))}
					data-testid={`batch-${action}`}
				>
					{LABEL[action]}
				</Button>
			{/each}
		</div>
	</div>
{/if}

{#if confirmStop}
	<Confirm title={m.jobs_stop_title()} text={m.batch_stop_text()} confirm={m.jobs_action_stop()} danger onconfirm={() => run('stop')} onclose={() => (confirmStop = false)} />
{/if}
