<script lang="ts">
	/**
	 * A mirror resync. Two ways in:
	 * - start now: one job per mailbox at once, each compares its own mailbox and keeps to the safety limit by itself
	 *   (a job over the limit makes no deletion and says why in its log); nothing to wait for here;
	 * - compare first: a comparison that writes nothing (dry run), one request per mailbox, a few at a time, each
	 *   mailbox shown as it is answered; Apply runs the mailboxes compared and ready so far. A deletion is confirmed
	 *   with its number. The mailboxes the server blocks are told why and how to fix it, and are left out of the run.
	 */
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import GitCompareArrows from '@lucide/svelte/icons/git-compare-arrows';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Play from '@lucide/svelte/icons/play';
	import Plus from '@lucide/svelte/icons/plus';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';
	import Hourglass from '@lucide/svelte/icons/hourglass';
	import ToggleRight from '@lucide/svelte/icons/toggle-right';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import type { Component } from 'svelte';
	import { onDestroy, onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { MirrorAccountPlan, MirrorAction, MirrorKind, MirrorPlan, MirrorRun, RunInput } from '../../lib/api/types.ts';
	import { fmtAbsolute, fmtNum, toDate } from '../../lib/format.ts';
	import { accountTotals, blockedBy, blockedPlan, comparedInOrder, eachLimited, MIRROR_COMPARE_PARALLEL, type BlockCode, blockCodes, blockInfo, canApply, clampCount, clampPercent, DEFAULT_MAX_DELETE_PERCENT, DEFAULT_MIN_DELETE_COUNT, type EmptyFolder, emptyFolderOf, hasWarningNote, mergeEmptyFolders, MAX_MIN_DELETE_COUNT, mirrorRows, mirrorState, mirrorTotals, neededPercent, offersLargeDelete, readyIds, sampleLines, startedJobs, toggleEmptyFolder } from '../../lib/logic/mirror.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import MirrorNotes from './MirrorNotes.svelte';

	interface Props {
		ids: number[];
		onclose: () => void;
		ondone: (ids: number[]) => void;
	}

	let { ids, onclose, ondone }: Props = $props();

	// One comparison per mailbox: its answer, its failure, or still on its way (by account id).
	let plans = $state.raw<ReadonlyMap<number, MirrorAccountPlan>>(new Map());
	let failures = $state.raw<ReadonlyMap<number, string>>(new Map());
	let pending = $state.raw<ReadonlySet<number>>(new Set());
	let percent = $state(DEFAULT_MAX_DELETE_PERCENT);
	let percentText = $state(String(DEFAULT_MAX_DELETE_PERCENT));
	let minCount = $state(DEFAULT_MIN_DELETE_COUNT);
	let minCountText = $state(String(DEFAULT_MIN_DELETE_COUNT));
	let allowLarge = $state(false);
	// The folders accepted empty (by key), and the empty folders seen so far: an accepted folder is no longer blocked, so it is remembered to be shown and taken back.
	let allowedEmpty = $state.raw<ReadonlySet<string>>(new Set());
	let emptyFolders = $state.raw<ReadonlyMap<string, EmptyFolder>>(new Map());
	let limitOpen = $state(false);
	let confirming = $state(false);
	let confirmingStart = $state(false);
	let applying = $state(false);
	let starting = $state(false);
	let applyError = $state<string | null>(null);
	let gone = false;
	// A new comparison of every mailbox (or a start) ends the round before it: its answers are no longer shown.
	let round = 0;
	onDestroy(() => (gone = true));

	const accounts = $derived(comparedInOrder(ids, plans));
	const comparing = $derived(pending.size > 0);
	const comparedCount = $derived(ids.length - pending.size);
	const busy = $derived(applying || starting);
	const verdict = $derived(mirrorState(accounts));
	// Start now is the next thing to do while there is nothing to apply yet; Apply once the comparison has something.
	const startFirst = $derived(comparing || !canApply(verdict));
	const totals = $derived(mirrorTotals(accounts));
	const runIds = $derived(readyIds(accounts));
	const codes = $derived(blockCodes(accounts));
	const blockedCount = $derived(accounts.filter((a) => !a.ready).length);
	const largeOffered = $derived(offersLargeDelete(accounts) || allowLarge);
	const needsEnable = $derived(blockedBy(accounts, 'mirror_off'));
	const tooMany = $derived(codes.includes('too_many_deletes'));
	const acceptedEmpty = $derived([...allowedEmpty].map((key) => emptyFolders.get(key)).filter((f): f is EmptyFolder => !!f));

	// The safety limit is where the way out of a refused deletion is: it opens by itself then.
	$effect(() => {
		if (tooMany) limitOpen = true;
	});

	const KIND = $derived<Record<MirrorKind, string>>({ mail: m.migrations_row_content_mail(), calendar: m.migrations_row_content_calendar(), contacts: m.migrations_row_content_contacts(), tasks: m.jobs_stream_tasks() });
	const ACTION = $derived<Record<MirrorAction, { label: string; icon: Component<{ class?: string; size?: number }>; tone: string }>>({
		add: { label: m.migrations_mirror_col_add(), icon: Plus, tone: 'text-signal-ink' },
		update: { label: m.migrations_mirror_col_update(), icon: Pencil, tone: 'text-info-ink' },
		delete: { label: m.migrations_mirror_col_delete(), icon: Trash2, tone: 'text-warning-ink' }
	});
	const BLOCK = $derived<Record<BlockCode, { reason: string; fix: string }>>({
		mirror_off: { reason: m.migrations_mirror_block_mirror_off(), fix: m.migrations_mirror_fix_mirror_off() },
		source_unlistable: { reason: m.migrations_mirror_block_source_unlistable(), fix: m.migrations_mirror_fix_source_unlistable() },
		too_many_deletes: { reason: m.migrations_mirror_block_too_many_deletes(), fix: m.migrations_mirror_fix_too_many_deletes() },
		source_empty: { reason: m.migrations_mirror_block_source_empty(), fix: m.migrations_mirror_fix_source_empty() },
		no_prior_run: { reason: m.migrations_mirror_block_no_prior_run(), fix: m.migrations_mirror_fix_no_prior_run() },
		uidvalidity_changed: { reason: m.migrations_mirror_block_uidvalidity_changed(), fix: m.migrations_mirror_fix_uidvalidity_changed() },
		target_unreachable: { reason: m.migrations_mirror_block_target_unreachable(), fix: m.migrations_mirror_fix_target_unreachable() },
		job_active: { reason: m.migrations_mirror_block_job_active(), fix: m.migrations_mirror_fix_job_active() },
		unknown_account: { reason: m.migrations_mirror_block_unknown_account(), fix: m.migrations_mirror_fix_unknown_account() }
	});
	const blockText = (code: string) => (blockInfo(code).known ? BLOCK[code as BlockCode] : undefined);

	/** Why Apply is off, in words: shown next to the button, and as its tooltip. */
	const reason = $derived(
		canApply(verdict)
			? ''
			: comparing || (!accounts.length && !failures.size)
				? m.migrations_mirror_apply_loading()
				: !accounts.length
					? m.migrations_mirror_compare_failed()
					: verdict === 'blocked'
						? m.migrations_mirror_apply_blocked()
						: verdict === 'uptodate'
							? m.migrations_mirror_apply_nothing()
							: ''
	);

	const body = (dry: boolean, accountIds: readonly number[]): RunInput => ({ account_ids: [...accountIds], mode: 'mirror', dry_run: dry, max_delete_percent: percent, min_delete_count: minCount, allow_large_delete: allowLarge, allow_empty_folders: [...allowedEmpty] });

	const without = <T,>(set: ReadonlySet<T>, item: T) => {
		const next = new Set(set);
		next.delete(item);
		return next;
	};

	/** Compares the mailboxes `only` names (a retry), or every one of them again (a new round: the options changed). */
	async function compare(only?: readonly number[]) {
		if (!only) {
			round++;
			plans = new Map();
			failures = new Map();
			pending = new Set();
		}
		const mine = round;
		const todo = (only ?? ids).filter((id) => !pending.has(id));
		if (!todo.length) return;
		pending = new Set([...pending, ...todo]);
		const left = new Map(failures);
		for (const id of todo) left.delete(id);
		failures = left;
		const stale = () => gone || mine !== round;
		await eachLimited(
			todo,
			MIRROR_COMPARE_PARALLEL,
			async (id) => {
				try {
					const answer = await post<MirrorPlan>('/runs', body(true, [id]));
					if (stale()) return;
					const own = answer.accounts?.find((a) => a.account_id === id) ?? answer.accounts?.[0];
					if (own) plans = new Map(plans).set(id, own);
					emptyFolders = mergeEmptyFolders(emptyFolders, comparedInOrder(ids, plans), allowedEmpty);
				} catch (e) {
					if (stale()) return;
					failures = new Map(failures).set(id, errorText(e));
				} finally {
					if (!stale()) pending = without(pending, id);
				}
			},
			stale
		);
	}

	onMount(() => void compare());

	function commitPercent() {
		const next = clampPercent(percentText);
		percentText = String(next);
		if (next === percent) return;
		percent = next;
		void compare();
	}

	function commitMinCount() {
		const next = clampCount(minCountText);
		minCountText = String(next);
		if (next === minCount) return;
		minCount = next;
		void compare();
	}

	function toggleEmpty(key: string, on: boolean) {
		allowedEmpty = toggleEmptyFolder(allowedEmpty, key, on);
		void compare();
	}

	function toggleLarge(checked: boolean) {
		allowLarge = checked;
		void compare();
	}

	async function enableAll() {
		await Promise.all(needsEnable.map((a) => data.setOption(a.account_id, 'mirror', true)));
		toast(m.migrations_mirror_enabled(), 'signal');
		void compare();
	}

	function apply() {
		if (totals.delete > 0) confirming = true;
		else void run();
	}

	/** Every selected mailbox, without waiting for the comparison: each job checks its own mailbox and keeps to the limits. */
	async function startAll() {
		starting = true;
		applyError = null;
		try {
			const { accounts: answered, jobs: created } = await post<MirrorRun>('/runs', body(false, ids));
			const jobs = startedJobs(created);
			for (const job of jobs) data.upsertJob(job);
			const started = new Set(jobs.map((job) => job.account_id));
			const left = (answered ?? []).filter((a) => !started.has(a.account_id));
			if (!jobs.length) {
				// Nothing started: the comparison stops, and each mailbox shows what blocked it.
				round++;
				pending = new Set();
				failures = new Map();
				plans = new Map(left.map((a) => [a.account_id, blockedPlan(a)]));
				return;
			}
			toast(m.migrations_mirror_queued({ count: jobs.length }), 'signal');
			if (left.length) toast(m.migrations_mirror_start_skipped({ count: left.length }), 'warning');
			ondone(jobs.map((job) => job.account_id));
		} catch (e) {
			applyError = errorText(e);
		} finally {
			starting = false;
		}
	}

	async function run() {
		applying = true;
		applyError = null;
		try {
			// The server answers 200 with a job per mailbox that may run; the others come back with their blocks and no job.
			const { jobs: answered } = await post<MirrorRun>('/runs', body(false, runIds));
			const jobs = startedJobs(answered);
			for (const job of jobs) data.upsertJob(job);
			if (!jobs.length) {
				void compare();
				return;
			}
			toast(m.migrations_mirror_queued({ count: jobs.length }), 'signal');
			ondone(jobs.map((job) => job.account_id));
		} catch (e) {
			applyError = errorText(e);
			// The destination may have changed since the comparison: show it again.
			void compare();
		} finally {
			applying = false;
		}
	}

	const emptySentence = (f: EmptyFolder) => (f.count === null ? m.migrations_mirror_empty_sentence_unknown() : m.migrations_mirror_empty_sentence({ count: count(f.count) }));
	const email = (a: MirrorAccountPlan) => prefs.email(a.email || `#${a.account_id}`);
	const when = (value: string) => (toDate(value) ? fmtAbsolute(toDate(value)) : value);
	const count = (n: number) => fmtNum(n);
</script>

{#snippet totalsText(t: { add: number; update: number; delete: number })}
	{m.migrations_mirror_totals({ add: count(t.add), update: count(t.update), delete: count(t.delete) })}
{/snippet}

<Dialog title={m.migrations_mirror_resync()} {onclose} wide>
	<div class="flex flex-col gap-3">
		<p class="m-0 flex items-center gap-2 text-ink-2" data-testid="mirror-readonly">
			<span class="text-signal-ink"><ShieldCheck size={16} aria-hidden="true" /></span>
			{m.migrations_mirror_never_writes()}
		</p>

		{#if comparing}
			<div class="flex flex-col gap-1.5" role="status" data-testid="mirror-loading">
				<p class="m-0 flex flex-wrap items-baseline justify-between gap-x-3 text-ink-2">
					<span>{m.migrations_mirror_comparing()}</span>
					<span class="font-mono text-xs tabular-nums" data-testid="mirror-progress">{m.migrations_mirror_progress({ done: count(comparedCount), total: count(ids.length) })}</span>
				</p>
				<Progress value={ids.length ? (comparedCount / ids.length) * 100 : 0} label={m.migrations_mirror_comparing()} />
			</div>
		{/if}

		{#if accounts.length}
			<div class="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="mirror-summary">
				<p class="m-0 font-medium" data-testid="mirror-totals">{@render totalsText(totals)}</p>
				{#if verdict === 'uptodate'}
					<Plate tone="signal" icon={CircleCheck} label={m.migrations_mirror_uptodate()} />
				{/if}
			</div>
			{#if blockedCount && verdict !== 'blocked'}
				<Notice><p class="m-0" data-testid="mirror-blocked-count">{m.migrations_mirror_blocked_skipped({ count: blockedCount })}</p></Notice>
			{/if}
			{#if needsEnable.length}
				<div><Button icon={ToggleRight} onclick={enableAll} data-testid="mirror-enable">{m.migrations_mirror_enable()}</Button></div>
			{/if}

		{/if}

		{#if ids.length}
			<ul class="m-0 flex list-none flex-col p-0" data-testid="mirror-accounts">
				{#each ids as id (id)}
					{@const account = plans.get(id)}
					{@const failure = failures.get(id)}
					{#if !account}
						<li class="border-b border-line" data-testid={failure ? 'mirror-account-failed' : 'mirror-account-pending'}>
							<div class="flex min-h-10 flex-wrap items-center gap-x-2 py-1">
								<span class="min-w-0 truncate font-mono text-xs font-medium">{prefs.email(data.accounts.get(id)?.email || `#${id}`)}</span>
								{#if failure}
									<Plate tone="warning" icon={TriangleAlert} label={m.migrations_mirror_compare_failed()} />
								{:else if pending.has(id)}
									<Plate icon={Hourglass} busy label={m.migrations_mirror_comparing_one()} />
								{/if}
							</div>
							{#if failure}
								<div class="pb-3 pl-4"><ErrorNotice title={m.migrations_mirror_compare_failed()} message={failure} onretry={() => compare([id])} /></div>
							{/if}
						</li>
					{:else}
					{@const own = accountTotals(account)}
					{@const rows = mirrorRows(account)}
					{@const lines = sampleLines(account)}
					<li class="border-b border-line" data-testid="mirror-account">
						<details open={ids.length <= 3 || !account.ready || hasWarningNote(account.notes)}>
							<summary class="flex min-h-10 cursor-pointer flex-wrap items-center gap-x-2 py-1">
								<span class="min-w-0 truncate font-mono text-xs font-medium">{email(account)}</span>
								<Plate tone={account.ready ? 'signal' : 'warning'} icon={account.ready ? CircleCheck : TriangleAlert} label={account.ready ? m.migrations_mirror_ready() : m.migrations_mirror_blocked()} />
								{#if ids.length > 1}<span class="text-xs text-ink-2">{@render totalsText(own)}</span>{/if}
							</summary>
							<div class="flex flex-col gap-3 pb-3 pl-4 text-xs">
								{#each account.blocked ?? [] as block, b (b)}
									{@const text = blockText(block.code)}
									{@const empty = emptyFolderOf(block)}
									{@const needed = neededPercent(block)}
									<div class="flex items-start gap-2 rounded-btn border border-warning/30 bg-warning-soft p-3 text-warning-ink" data-testid={`mirror-block-${block.code}`}>
										<span class="mt-0.5"><CircleAlert size={14} aria-hidden="true" /></span>
										<div class="min-w-0">
											<p class="m-0 font-semibold">{text?.reason ?? prefs.text(block.message)}</p>
											{#if text && block.message}<p class="m-0 mt-1 font-mono break-words text-ink-2">{prefs.text(block.message)}</p>{/if}
											{#if text}<p class="m-0 mt-1 text-ink">{text.fix}</p>{/if}
											{#if needed !== null}<p class="m-0 mt-1 text-ink" data-testid="mirror-needed-percent">{m.migrations_mirror_needed_percent({ percent: needed })}</p>{/if}
											{#if empty}
												<div class="mt-2 flex flex-col items-start gap-2" data-testid="mirror-empty-folder">
													<p class="m-0 text-ink">
														<span class="font-mono font-medium">{prefs.text(empty.key)}</span>
														{emptySentence(empty)}
													</p>
													<Button onclick={() => toggleEmpty(empty.key, true)} data-testid="mirror-empty-allow">{m.migrations_mirror_empty_allow()}</Button>
												</div>
											{/if}
										</div>
									</div>
								{/each}
								{#if rows.length}
									<div class="overflow-x-auto">
										<table class="w-full" data-testid="mirror-counts">
											<thead>
												<tr class="text-left text-ink-2">
													<th class="py-1 pr-2 font-medium">{m.jobs_report_type()}</th>
													<th class="py-1 pr-2 text-right font-medium">{m.migrations_mirror_col_add()}</th>
													<th class="py-1 pr-2 text-right font-medium">{m.migrations_mirror_col_update()}</th>
													<th class="py-1 text-right font-medium">{m.migrations_mirror_col_delete()}</th>
												</tr>
											</thead>
											<tbody class="font-mono">
												{#each rows as row (row.kind)}
													<tr class="border-t border-line" data-testid={`mirror-row-${row.kind}`}>
														<td class="py-1 pr-2 font-sans">{KIND[row.kind]}</td>
														<td class="py-1 pr-2 text-right">{count(row.add)}</td>
														<td class="py-1 pr-2 text-right">{count(row.update)}</td>
														<td class={`py-1 text-right ${row.delete ? 'font-semibold text-warning-ink' : ''}`}>{count(row.delete)}</td>
													</tr>
												{/each}
											</tbody>
										</table>
									</div>
								{:else if account.ready}
									<p class="m-0 text-ink-2">{m.migrations_mirror_uptodate()}</p>
								{/if}
								{#if account.notes?.length}
									<div class="flex flex-col gap-2" data-testid="mirror-notes"><MirrorNotes notes={account.notes} /></div>
								{/if}
								{#if lines.length}
									<Disclosure title={m.migrations_mirror_sample({ shown: lines.length, total: count(own.add + own.update + own.delete) })} data-testid="mirror-sample">
										<ul class="m-0 flex list-none flex-col p-0">
											{#each lines as line, i (i)}
												{@const info = ACTION[line.action]}
												{@const Icon = info.icon}
												<li class="flex min-h-10 items-center gap-2 border-t border-line first:border-t-0" data-testid="mirror-sample-line">
													<span class={`inline-flex shrink-0 ${info.tone}`} title={info.label}><Icon size={14} /></span>
													<span class="sr-only">{info.label}</span>
													<span class="w-16 shrink-0 text-ink-2">{KIND[line.kind] ?? line.kind}</span>
													<span class="min-w-0 flex-1 truncate" title={`${line.folder} / ${line.subject}`}>
														<span class="font-mono text-ink-3">{prefs.text(line.folder)}</span>
														{prefs.text(line.subject)}
													</span>
													<span class="hidden shrink-0 text-ink-3 sm:inline">{when(line.date)}</span>
												</li>
											{/each}
										</ul>
									</Disclosure>
								{/if}
							</div>
						</details>
					</li>
					{/if}
				{/each}
			</ul>
		{/if}

		{#if acceptedEmpty.length}
			<div class="flex flex-col gap-1" data-testid="mirror-empty-accepted">
				{#each acceptedEmpty as folder (folder.key)}
					<div class="flex flex-col">
						<label class="flex min-h-10 items-center gap-2">
							<span class="check"><input type="checkbox" checked onchange={() => toggleEmpty(folder.key, false)} data-testid="mirror-empty-accepted-item" data-folder={folder.key} /></span>
							{m.migrations_mirror_empty_allowed({ folder: prefs.text(folder.key) })}
						</label>
						<p class="m-0 pl-7 text-xs text-ink-2">{emptySentence(folder)}</p>
					</div>
				{/each}
			</div>
		{/if}

		<Disclosure title={m.migrations_mirror_limit()} summary={m.migrations_mirror_limit_value({ percent })} bind:open={limitOpen} data-testid="mirror-limit">
			<div class="flex flex-col gap-3 pt-1">
				<Field label={m.migrations_mirror_limit_label()} hint={m.migrations_mirror_limit_hint()}>
					<input class="input w-24 font-mono" type="number" min="1" max="100" step="1" inputmode="numeric" bind:value={percentText} onchange={commitPercent} data-testid="mirror-percent" />
				</Field>
				<Field label={m.migrations_mirror_min_count_label()} hint={m.migrations_mirror_min_count_hint()}>
					<input class="input w-24 font-mono" type="number" min="0" max={MAX_MIN_DELETE_COUNT} step="1" inputmode="numeric" bind:value={minCountText} onchange={commitMinCount} data-testid="mirror-min-count" />
				</Field>
			</div>
		</Disclosure>

		{#if largeOffered}
			<div class="flex flex-col gap-2" data-testid="mirror-large">
				<label class="flex min-h-10 items-center gap-2">
					<span class="check"><input type="checkbox" checked={allowLarge} onchange={(e) => toggleLarge(e.currentTarget.checked)} data-testid="mirror-large-allow" /></span>
					{m.migrations_mirror_large()}
				</label>
				<Notice><p class="m-0">{m.migrations_mirror_large_warning()}</p></Notice>
			</div>
		{/if}

		{#if applyError}<ErrorNotice title={m.migrations_mirror_apply_failed()} message={applyError} />{/if}
		<p class="m-0 text-xs text-ink-2" data-testid="mirror-start-hint">{m.migrations_mirror_start_hint()}</p>
		{#if reason && !comparing}<p class="m-0 text-xs text-ink-2" data-testid="mirror-apply-reason">{reason}</p>{/if}
		{#if comparing && canApply(verdict)}<p class="m-0 text-xs text-ink-2" data-testid="mirror-apply-partial">{m.migrations_mirror_apply_partial({ count: count(runIds.length) })}</p>{/if}
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button icon={ScanSearch} loading={comparing} disabled={busy} onclick={() => compare()} data-testid="mirror-compare">{m.migrations_mirror_compare_again()}</Button>
		<Button variant={startFirst ? 'primary' : 'secondary'} icon={Play} loading={starting} disabled={applying} onclick={() => (confirmingStart = true)} data-testid="mirror-start">{m.migrations_mirror_start_now({ count: count(ids.length) })}</Button>
		<Button
			variant={totals.delete > 0 ? 'danger' : startFirst ? 'secondary' : 'primary'}
			icon={GitCompareArrows}
			loading={applying}
			disabled={starting || !canApply(verdict)}
			{reason}
			onclick={apply}
			data-testid="mirror-apply"
		>
			{m.migrations_mirror_apply()}
		</Button>
	{/snippet}
</Dialog>

{#if confirmingStart}
	<Confirm
		title={m.migrations_mirror_start_confirm_title({ count: count(ids.length) })}
		text={m.migrations_mirror_start_confirm_text({ percent, min: count(minCount) })}
		confirm={m.migrations_mirror_start_confirm_action({ count: count(ids.length) })}
		onconfirm={startAll}
		onclose={() => (confirmingStart = false)}
	/>
{/if}

{#if confirming}
	<Confirm
		title={m.migrations_mirror_confirm_title({ count: count(totals.delete) })}
		text={m.migrations_mirror_confirm_text({ count: count(totals.delete) })}
		confirm={m.migrations_mirror_confirm_action({ count: count(totals.delete) })}
		danger
		onconfirm={run}
		onclose={() => (confirming = false)}
	/>
{/if}
