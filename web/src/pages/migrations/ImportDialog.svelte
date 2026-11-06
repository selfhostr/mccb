<script lang="ts">
	/** Says what will start and what will not, before anything happens. */
	import Play from '@lucide/svelte/icons/play';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { Job } from '../../lib/api/types.ts';
	import type { ImportPlan } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	interface Props {
		plan: ImportPlan;
		onclose: () => void;
		ondone: (ids: number[]) => void;
	}

	let { plan, onclose, ondone }: Props = $props();
	let busy = $state(false);
	let error = $state<string | null>(null);

	const problems = $derived([
		{ key: 'pst', text: m.migrations_import_no_pst({ count: plan.noPst.length }), list: plan.noPst },
		{ key: 'password', text: m.migrations_import_no_password({ count: plan.noPassword.length }), list: plan.noPassword },
		{ key: 'active', text: m.migrations_import_active({ count: plan.active.length }), list: plan.active }
	].filter((p) => p.list.length));

	async function start() {
		busy = true;
		error = null;
		const ids = plan.ready.map((a) => a.id);
		try {
			const jobs = await post<Job[]>('/runs', { account_ids: ids });
			for (const job of jobs) data.upsertJob(job);
			toast(m.migrations_import_queued({ count: jobs.length }), 'signal');
			ondone(ids);
		} catch (e) {
			error = errorText(e);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.migrations_import_title()} {onclose}>
	<div class="flex flex-col gap-3">
		<p class="m-0">{plan.ready.length ? m.migrations_import_ready({ count: plan.ready.length }) : m.migrations_import_none()}</p>
		{#each problems as problem (problem.key)}
			<Notice>
				<p class="m-0">{problem.text}</p>
				<p class="m-0 mt-1 font-mono text-xs">{problem.list.slice(0, 5).map((a) => prefs.email(a.email)).join(', ')}</p>
			</Notice>
		{/each}
		{#if error}<ErrorNotice title={m.migrations_import_failed()} message={error} onretry={start} />{/if}
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" icon={Play} loading={busy} disabled={!plan.ready.length} onclick={start} data-testid="import-confirm">{m.migrations_import_start()}</Button>
	{/snippet}
</Dialog>
