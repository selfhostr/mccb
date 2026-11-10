<script lang="ts">
	/**
	 * Last look before the jobs exist: how many mailboxes, what goes where, and the loud warning again when the
	 * server takes no calendars or contacts. The batch gets a name, so the list can show these jobs alone.
	 */
	import Calendar from '@lucide/svelte/icons/calendar';
	import Contact from '@lucide/svelte/icons/contact';
	import ListTodo from '@lucide/svelte/icons/list-todo';
	import Mail from '@lucide/svelte/icons/mail';
	import Play from '@lucide/svelte/icons/play';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import type { CoverageState } from '../../lib/api/types.ts';
	import { COVERAGE_KINDS, type CoverageSummary } from '../../lib/logic/tenantmatch.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { COVERAGE_STATES, coverageLook, kindName } from './looks.ts';

	interface Props {
		mode: 'full' | 'delta';
		count: number;
		tenant: string;
		server: string;
		noDav: boolean;
		summary: CoverageSummary;
		defaultName: string;
		onconfirm: (batchName: string) => Promise<unknown>;
		onclose: () => void;
	}

	let { mode, count, tenant, server, noDav, summary, defaultName, onconfirm, onclose }: Props = $props();

	const ICONS = { mail: Mail, calendar: Calendar, contacts: Contact, tasks: ListTodo };
	// svelte-ignore state_referenced_locally
	let name = $state(defaultName);
	let busy = $state(false);
	let error = $state<string | null>(null);

	/** "12 of 12", then why the others will not get it. */
	function detail(counts: Record<CoverageState, number>): string {
		const why = COVERAGE_STATES.filter((s) => s !== 'on' && counts[s] > 0).map((s) => m.tenant_start_why({ count: counts[s], reason: coverageLook(s).label }));
		return [m.tenant_start_of({ count: counts.on, total: count }), ...why].join(', ');
	}

	async function run(e: SubmitEvent) {
		e.preventDefault();
		if (busy) return;
		busy = true;
		error = null;
		try {
			await onconfirm(name.trim() || defaultName);
			onclose();
		} catch (err) {
			// The dialog stays for another try, and says why.
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={mode === 'delta' ? m.tenant_resync_title() : m.tenant_start_title()} {onclose} wide>
	<form id="tenant-start" class="flex flex-col gap-3" onsubmit={run} data-testid="tenant-start-dialog">
		<p class="m-0">{mode === 'delta' ? m.tenant_resync_text({ count, tenant, server }) : m.tenant_start_text({ count, tenant, server })}</p>

		{#if noDav}
			<div class="flex items-start gap-2 rounded-btn border border-warning/40 bg-warning-soft p-3 text-warning-ink" role="alert">
				<span class="mt-0.5"><TriangleAlert size={16} /></span>
				<p class="m-0 font-semibold">{m.tenant_banner_no_dav_title({ server })}</p>
			</div>
		{/if}

		<ul class="m-0 flex list-none flex-col gap-1.5 rounded-btn border border-line p-3" aria-label={m.tenant_cov_label()} data-testid="tenant-start-coverage">
			{#each COVERAGE_KINDS as kind (kind)}
				{@const Icon = ICONS[kind]}
				{@const counts = summary[kind]}
				<li class="flex items-start gap-2 text-sm" data-kind={kind}>
					<span class={`mt-0.5 ${counts.on === count ? 'text-signal-ink' : counts.on ? 'text-advisory-ink' : 'text-ink-3'}`}><Icon size={16} aria-hidden="true" /></span>
					<span class="min-w-0"><span class="font-medium">{kindName(kind)}</span>: <span class="text-ink-2">{detail(counts)}</span></span>
				</li>
			{/each}
		</ul>

		<Field label={m.tenant_start_batch()} hint={m.tenant_start_batch_hint()}>
			<input class="input" type="text" maxlength="120" bind:value={name} data-testid="tenant-batch-name" />
		</Field>
		{#if error}<ErrorNotice title={m.tenant_start_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="tenant-start" icon={mode === 'delta' ? RefreshCw : Play} loading={busy} data-testid="tenant-start-confirm">
			{mode === 'delta' ? m.tenant_bar_resync() : m.tenant_bar_start()}
		</Button>
	{/snippet}
</Dialog>
