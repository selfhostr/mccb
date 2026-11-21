<script lang="ts">
	/**
	 * A mirror resync: first a comparison that writes nothing (dry run), then, once the reader has seen what
	 * would be added, changed and deleted, the run itself. A deletion is confirmed with its number.
	 * The mailboxes the server blocks are told why and how to fix it, and are left out of the run.
	 */
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import GitCompareArrows from '@lucide/svelte/icons/git-compare-arrows';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Plus from '@lucide/svelte/icons/plus';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';
	import ToggleRight from '@lucide/svelte/icons/toggle-right';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import type { Component } from 'svelte';
	import { onDestroy, onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { MirrorAccountPlan, MirrorAction, MirrorKind, MirrorPlan, MirrorRun, RunInput } from '../../lib/api/types.ts';
	import { fmtAbsolute, fmtNum, toDate } from '../../lib/format.ts';
	import { accountTotals, blockedBy, type BlockCode, blockCodes, blockInfo, canApply, clampCount, clampPercent, DEFAULT_MAX_DELETE_PERCENT, DEFAULT_MIN_DELETE_COUNT, type EmptyFolder, emptyFolderOf, hasWarningNote, mergeEmptyFolders, MAX_MIN_DELETE_COUNT, mirrorRows, mirrorState, mirrorTotals, neededPercent, offersLargeDelete, readyIds, sampleLines, startedJobs, toggleEmptyFolder } from '../../lib/logic/mirror.ts';
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
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import MirrorNotes from './MirrorNotes.svelte';

	interface Props {
		ids: number[];
		onclose: () => void;
		ondone: (ids: number[]) => void;
	}

	let { ids, onclose, ondone }: Props = $props();

	let plan = $state.raw<MirrorPlan | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
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
	let applying = $state(false);
	let applyError = $state<string | null>(null);
	let gone = false;
	let asked = 0;
	onDestroy(() => (gone = true));

	const accounts = $derived(plan?.accounts ?? []);
	const verdict = $derived(mirrorState(accounts));
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
		loading || (!plan && !error)
			? m.migrations_mirror_apply_loading()
			: error
				? m.migrations_mirror_compare_failed()
				: verdict === 'blocked'
					? m.migrations_mirror_apply_blocked()
					: verdict === 'uptodate'
						? m.migrations_mirror_apply_nothing()
						: ''
	);

	const body = (dry: boolean): RunInput => ({ account_ids: dry ? ids : runIds, mode: 'mirror', dry_run: dry, max_delete_percent: percent, min_delete_count: minCount, allow_large_delete: allowLarge, allow_empty_folders: [...allowedEmpty] });

	async function compare() {
		const mine = ++asked;
		loading = true;
		error = null;
		try {
			const answer = await post<MirrorPlan>('/runs', body(true));
			if (gone || mine !== asked) return;
			plan = answer;
			emptyFolders = mergeEmptyFolders(emptyFolders, answer.accounts ?? [], allowedEmpty);
		} catch (e) {
			if (gone || mine !== asked) return;
			plan = null;
			error = errorText(e);
		} finally {
			if (mine === asked) loading = false;
		}
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

	async function run() {
		applying = true;
		applyError = null;
		try {
			// The server answers 200 with a job per mailbox that may run; the others come back with their blocks and no job.
			const { jobs: answered } = await post<MirrorRun>('/runs', body(false));
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

		{#if error}<ErrorNotice title={m.migrations_mirror_compare_failed()} message={error} onretry={compare} />{/if}
		{#if loading}<p class="m-0 text-ink-2" role="status" data-testid="mirror-loading">{m.migrations_mirror_comparing()}</p>{/if}

		{#if plan && !loading}
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

			<ul class="m-0 flex list-none flex-col p-0" data-testid="mirror-accounts">
				{#each accounts as account (account.account_id)}
					{@const own = accountTotals(account)}
					{@const rows = mirrorRows(account)}
					{@const lines = sampleLines(account)}
					<li class="border-b border-line" data-testid="mirror-account">
						<details open={accounts.length <= 3 || !account.ready || hasWarningNote(account.notes)}>
							<summary class="flex min-h-10 cursor-pointer flex-wrap items-center gap-x-2 py-1">
								<span class="min-w-0 truncate font-mono text-xs font-medium">{email(account)}</span>
								<Plate tone={account.ready ? 'signal' : 'warning'} icon={account.ready ? CircleCheck : TriangleAlert} label={account.ready ? m.migrations_mirror_ready() : m.migrations_mirror_blocked()} />
								{#if accounts.length > 1}<span class="text-xs text-ink-2">{@render totalsText(own)}</span>{/if}
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
		{#if reason && !loading}<p class="m-0 text-xs text-ink-2" data-testid="mirror-apply-reason">{reason}</p>{/if}
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button icon={ScanSearch} loading={loading} disabled={applying} onclick={compare} data-testid="mirror-compare">{m.migrations_mirror_compare_again()}</Button>
		<Button
			variant={totals.delete > 0 ? 'danger' : 'primary'}
			icon={GitCompareArrows}
			loading={applying}
			disabled={loading || !canApply(verdict)}
			{reason}
			onclick={apply}
			data-testid="mirror-apply"
		>
			{m.migrations_mirror_apply()}
		</Button>
	{/snippet}
</Dialog>

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
