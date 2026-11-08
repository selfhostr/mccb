<script lang="ts">
	/**
	 * One pair: the Microsoft 365 mailbox on the left, the state in the middle, the server mailbox on the right,
	 * then what will go. Wide screens read it as a table row; on a phone the two sides stack.
	 */
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import ArrowRight from '@lucide/svelte/icons/arrow-right';
	import AtSign from '@lucide/svelte/icons/at-sign';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import DoorOpen from '@lucide/svelte/icons/door-open';
	import UserRoundX from '@lucide/svelte/icons/user-round-x';
	import Users from '@lucide/svelte/icons/users';
	import Wrench from '@lucide/svelte/icons/wrench';
	import { m } from '#lib/paraglide/messages.js';
	import type { MatchRow } from '../../lib/api/types.ts';
	import { connectionOf } from '../../lib/logic/accounts.ts';
	import { isTickable } from '../../lib/logic/tenantmatch.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { jobLookOf, jobLook, testLook, type StatusLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Plate from '../../lib/ui/Plate.svelte';
	import Coverage from './Coverage.svelte';
	import { matchLook } from './looks.ts';

	interface Props {
		row: MatchRow;
		server: string;
		ticked: boolean;
		/** Why the last Start left this mailbox out. */
		problem?: string;
		ontoggle: (checked: boolean) => void;
	}

	let { row, server, ticked, problem, ontoggle }: Props = $props();

	const box = $derived(row.mailbox);
	const tickable = $derived(isTickable(row));
	const look = $derived(matchLook(row.state, server));
	// The stream keeps accounts and jobs current: a test or a job started elsewhere shows here at once.
	const account = $derived(row.account ? (data.accounts.get(row.account.id) ?? row.account) : null);
	const job = $derived(row.account ? (data.jobs.get(row.account.id) ?? null) : null);
	const jobLink = $derived(job ? { id: job.id, look: jobLookOf(job) } : row.last_job ? { id: row.last_job.id, look: jobLook(row.last_job.status) } : null);
	const phase = $derived(row.account ? data.testing.get(row.account.id) : undefined);
	const name = $derived(prefs.email(box?.mail || row.account?.email || ''));

	const check = $derived.by((): StatusLook | null => {
		if (!account) return null;
		if (phase) return testLook(phase);
		const c = connectionOf(account);
		if (c === 'ok') return { tone: 'signal', icon: CircleCheck, label: m.migrations_test_status_ok() };
		if (c === 'untested') return { tone: 'ghost', icon: CircleDashed, label: m.migrations_test_never() };
		return { tone: 'warning', icon: CircleX, label: account.status === 'auth_failed' ? m.migrations_row_login_refused() : m.migrations_test_status_failed() };
	});

	const plates = $derived.by((): StatusLook[] => {
		const out: StatusLook[] = [];
		if (!box) return out;
		if (box.kind === 'shared') out.push({ tone: 'ghost', icon: Users, label: m.tenant_plate_shared() });
		if (box.kind === 'room') out.push({ tone: 'ghost', icon: DoorOpen, label: m.tenant_plate_room() });
		if (box.kind === 'equipment') out.push({ tone: 'ghost', icon: Wrench, label: m.tenant_plate_equipment() });
		if (!box.enabled) out.push({ tone: 'advisory', icon: UserRoundX, label: m.tenant_plate_disabled() });
		if (row.matched_by === 'alias' && row.matched_address) out.push({ tone: 'info', icon: AtSign, label: m.tenant_plate_via_alias({ address: prefs.email(row.matched_address) }) });
		if (row.matched_by === 'upn' && row.matched_address) out.push({ tone: 'info', icon: AtSign, label: m.tenant_plate_via_upn({ address: prefs.email(row.matched_address) }) });
		return out;
	});
</script>

<li
	class={`grid grid-cols-[2.5rem_minmax(0,1fr)] items-center gap-x-2 gap-y-1 px-3 py-2 md:grid-cols-[2.5rem_minmax(0,1.15fr)_11rem_minmax(0,1fr)_7.5rem] md:gap-x-3 ${tickable ? '' : 'bg-surface-2/40'}`}
	data-testid="tenant-row"
	data-state={row.state}
	data-mailbox={box?.mail ?? ''}
	data-account={row.account?.email ?? ''}
	data-account-id={row.account?.id ?? ''}
>
	<span class="row-span-4 self-start md:row-span-1 md:self-center">
		{#if tickable}
			<label class="check">
				<input type="checkbox" checked={ticked} onchange={(e) => ontoggle(e.currentTarget.checked)} aria-label={m.tenant_row_tick({ email: name })} data-testid="tenant-tick" />
			</label>
		{/if}
	</span>

	<!-- Microsoft 365 side -->
	<div class="min-w-0">
		{#if box}
			<p class="m-0 truncate font-semibold" title={prefs.text(box.display_name)}>{prefs.text(box.display_name) || prefs.email(box.mail)}</p>
			<p class="m-0 truncate font-mono text-xs text-ink-2">{prefs.email(box.mail)}</p>
			{#if plates.length}
				<p class="m-0 mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
					{#each plates as p, i (i)}<Plate tone={p.tone} icon={p.icon} label={p.label} />{/each}
				</p>
			{/if}
		{:else}
			<p class="m-0 text-sm text-ink-3 italic">{m.tenant_row_no_mailbox()}</p>
		{/if}
	</div>

	<!-- State, with the direction of the move -->
	<div class="col-start-2 flex min-w-0 items-center gap-1.5 md:col-start-auto" data-testid="tenant-state">
		<span class="text-ink-3 md:hidden" aria-hidden="true"><ArrowDown size={14} /></span>
		<span class="hidden text-ink-3 md:inline-flex" aria-hidden="true"><ArrowRight size={14} /></span>
		<Plate tone={look.tone} icon={look.icon} label={look.label} title={look.label} />
	</div>

	<!-- Server side -->
	<div class="col-start-2 min-w-0 md:col-start-auto">
		{#if account}
			<p class="m-0 truncate font-mono text-xs text-ink md:text-sm">{prefs.email(account.email)}</p>
			<p class="m-0 mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
				{#if check}<span data-testid="tenant-check"><Plate tone={check.tone} icon={check.icon} label={check.label} /></span>{/if}
				{#if row.state === 'other_source'}
					<a class="text-xs" href={`/?account=${account.id}`}>{m.tenant_row_see_source()}</a>
				{/if}
				{#if problem}<span class="text-xs text-warning-ink" role="alert" data-testid="tenant-row-problem">{problem}</span>{/if}
				{#if row.state === 'conflict' && row.conflict_with}
					<span class="truncate text-xs text-ink-2">{m.tenant_row_conflict_with({ address: prefs.email(row.conflict_with) })}</span>
				{/if}
			</p>
		{:else}
			<p class="m-0 text-sm text-ink-3 italic">{m.tenant_row_add_to_csv({ server })}</p>
		{/if}
	</div>

	<!-- What will go, and the last job -->
	<div class="col-start-2 flex flex-wrap items-center gap-x-3 gap-y-1 md:col-start-auto md:flex-col md:items-start md:gap-1">
		{#if box && account}<Coverage coverage={row.coverage} />{/if}
		{#if jobLink && (row.state === 'linked' || job)}
			<a class="no-underline hover:underline" href={`/jobs/${jobLink.id}`} data-testid="tenant-job">
				<Plate tone={jobLink.look.tone} icon={jobLink.look.icon} label={jobLink.look.label} />
			</a>
		{/if}
	</div>
</li>
