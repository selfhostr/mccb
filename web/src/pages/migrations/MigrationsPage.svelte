<script lang="ts">
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import SearchX from '@lucide/svelte/icons/search-x';
	import SlidersHorizontal from '@lucide/svelte/icons/sliders-horizontal';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, del } from '../../lib/api/client.ts';
	import type { Account } from '../../lib/api/types.ts';
	import { fmtLeft } from '../../lib/format.ts';
	import { connectionCounts, importPlan, type ImportPlan, matchesFilter, overallProgress, rowRank, type StatusFilter, STATUS_FILTERS, summarize } from '../../lib/logic/accounts.ts';
	import { FILE_ACCEPT } from '../../lib/logic/pst.ts';
	import { headerState, splitSelection, toggleAllVisible } from '../../lib/logic/selection.ts';
	import { VIRTUAL_AT, windowRange } from '../../lib/logic/virtual.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import { slide } from '../../lib/ui/motion.ts';
	import Notice from '../../lib/ui/Notice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import AccountDialog from './AccountDialog.svelte';
	import AccountPanel from './AccountPanel.svelte';
	import AccountRow from './AccountRow.svelte';
	import AccountSettings from './AccountSettings.svelte';
	import BatchBar from './BatchBar.svelte';
	import CsvDialog from './CsvDialog.svelte';
	import ImportDialog from './ImportDialog.svelte';
	import GettingStarted from './GettingStarted.svelte';
	import JobPanel from './JobPanel.svelte';
	import ServerDialog from '../settings/ServerDialog.svelte';
	import RouteDialog from './RouteDialog.svelte';
	import SelectionBar from './SelectionBar.svelte';
	import TestDialog from './TestDialog.svelte';

	const q = $derived((router.params.get('q') ?? '').trim().toLowerCase());
	const server = $derived(Number(router.params.get('server')) || null);
	const status = $derived.by<StatusFilter>(() => {
		const value = router.params.get('status') as StatusFilter | null;
		return value && STATUS_FILTERS.includes(value) ? value : '';
	});
	const batch = $derived.by(() => {
		const id = Number(router.params.get('batch')) || null;
		return id && data.batches.some((b) => b.id === id) ? id : null;
	});
	const hasFilter = $derived(!!(q || server || status || batch));

	const all = $derived([...data.accounts.values()].sort((a, b) => a.email.localeCompare(b.email)));

	// What filter, order and counts read from the jobs, as one string. Progress does not change it,
	// so a progress event does not refilter or reorder the list: only a change of status does.
	const shape = $derived.by(() => {
		let text = '';
		for (const account of all) {
			const job = data.jobs.get(account.id);
			if (job) text += `${account.id}:${job.status}:${job.batch_id ?? ''};`;
		}
		return text;
	});
	const jobOf = (id: number) => untrack(() => data.jobs.get(id));

	/** Passes search, server and batch; the state is left to the chips, whose counts come from here. */
	const scoped = $derived.by(() => {
		void shape;
		const filter = { q, server, status: '' as const, batch };
		return all.filter((a) => matchesFilter(a, jobOf(a.id), filter));
	});
	const counts = $derived.by(() => {
		void shape;
		return summarize(scoped, jobOf);
	});
	const conn = $derived(connectionCounts(scoped));
	const CONN_CHIPS = $derived([
		{ value: 'tested_ok' as const, label: m.migrations_conn_ok({ count: conn.ok }), icon: CircleCheck, tone: 'text-signal-ink' },
		{ value: 'test_failed' as const, label: m.migrations_conn_failed({ count: conn.failed }), icon: CircleX, tone: 'text-warning-ink' },
		{ value: 'untested' as const, label: m.migrations_conn_untested({ count: conn.untested }), icon: CircleDashed, tone: 'text-ink-3' }
	]);
	const summary = $derived.by(() => {
		void shape;
		return summarize(all, jobOf);
	});
	const rows = $derived.by(() => {
		void shape;
		const shown = status ? scoped.filter((a) => matchesFilter(a, jobOf(a.id), { q: '', server: null, status })) : scoped;
		// Sorted by email already, and the sort is stable.
		return shown
			.map((account) => ({ account, rank: rowRank(account, jobOf(account.id)) }))
			.sort((a, b) => a.rank - b.rank)
			.map((item) => item.account);
	});
	const visibleIds = $derived(rows.map((a) => a.id));

	// These read live job progress: the bar and the sentence only change when their text or value does.
	const overall = $derived(overallProgress(all, (id) => data.jobs.get(id)));
	const overallPercent = $derived(overall.percent);
	const left = $derived(overall.left);

	const sentence = $derived.by(() => {
		if (data.loading) return m.common_loading();
		const { done, total, attention } = summary;
		const first = attention === 0 ? m.migrations_page_summary({ done, total }) : attention === 1 ? m.migrations_page_summary_one({ done, total }) : m.migrations_page_summary_many({ done, total, attention });
		if (left == null) return first;
		const time = fmtLeft(left);
		return `${first} ${time ? m.migrations_page_left({ time }) : m.migrations_page_left_soon()}`;
	});

	const CHIPS = $derived([
		{ value: '' as const, label: m.migrations_filter_all(), count: counts.total },
		{ value: 'problem' as const, label: m.migrations_filter_problem(), count: counts.attention },
		{ value: 'active' as const, label: m.migrations_filter_active(), count: counts.running },
		{ value: 'done' as const, label: m.migrations_filter_done(), count: counts.done }
	]);

	// Everything the chips do not cover waits behind one quiet button, and shows by itself when in use.
	const hiddenFilter = $derived(!!(server || batch || status === 'no_pst'));
	let moreOpen = $state(false);
	$effect(() => {
		if (hiddenFilter) moreOpen = true;
	});

	const header = $derived(headerState(visibleIds, data.selected));
	const split = $derived(splitSelection(data.selected, visibleIds));
	const testingCount = $derived(split.targets.filter((id) => data.testing.has(id)).length);

	// A long list renders only what is on screen, plus a margin. Row height is measured, not assumed.
	let list = $state<HTMLElement>();
	let offset = $state(0);
	let viewport = $state(typeof innerHeight === 'number' ? innerHeight : 800);
	let rowHeight = $state(56);
	const range = $derived(windowRange(rows.length, offset, viewport, rowHeight));
	const windowed = $derived(rows.slice(range.start, range.end));

	let scrollPlanned = false;
	function measure() {
		scrollPlanned = false;
		if (!list) return;
		offset = -list.getBoundingClientRect().top;
		viewport = innerHeight;
		const row = list.querySelector<HTMLElement>('.acct-row');
		if (row && row.offsetHeight) rowHeight = row.offsetHeight;
	}
	function onScroll() {
		if (scrollPlanned || rows.length <= VIRTUAL_AT) return;
		scrollPlanned = true;
		requestAnimationFrame(measure);
	}
	$effect(() => {
		// Measure again when the list changes shape.
		void rows.length;
		void prefs.value.density;
		void moreOpen;
		requestAnimationFrame(measure);
	});

	let dialog = $state.raw<
		| { kind: 'add' }
		| { kind: 'csv' }
		| { kind: 'edit'; account: Account }
		| { kind: 'delete'; account: Account }
		| { kind: 'details'; id: number }
		| { kind: 'route'; account: Account }
		| { kind: 'import'; plan: ImportPlan }
		| { kind: 'server' }
		| null
	>(null);
	const closeDialog = () => (dialog = null);

	function clearFilters() {
		router.setParams({ q: null, server: null, status: null, batch: null });
	}

	function testSelected() {
		data.test(split.targets);
	}

	function openImport() {
		const chosen = split.targets.map((id) => data.accounts.get(id)).filter((a): a is Account => !!a);
		dialog = {
			kind: 'import',
			plan: importPlan(chosen, (id) => data.jobs.get(id), (id) => data.psts.get(id))
		};
	}

	function imported(ids: number[]) {
		for (const id of ids) data.selected.delete(id);
		closeDialog();
	}

	async function removeAccount(account: Account) {
		try {
			await del(`/accounts/${account.id}`);
			data.accounts.delete(account.id);
			data.selected.delete(account.id);
			toast(m.migrations_row_deleted({ email: prefs.email(account.email) }), 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}

	function uploadFor(id: number) {
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = FILE_ACCEPT;
		input.onchange = () => {
			const file = input.files?.[0];
			if (!file) return;
			uploads.add([file], 'size-asc', id);
			toast(m.migrations_row_upload_queued(), 'info');
		};
		input.click();
	}

	// The panel opens on a job (/jobs/3) or, before there is one, on a mailbox (?account=3).
	const panelJob = $derived(router.route.jobId);
	const panelAccount = $derived.by(() => {
		const id = Number(router.params.get('account')) || null;
		return panelJob == null && id != null && data.accounts.has(id) ? id : null;
	});
	function closePanel() {
		const selector = panelJob != null ? `[data-job-link="${panelJob}"]` : `[data-account-link="${panelAccount}"]`;
		const params = new URLSearchParams(location.search);
		params.delete('tab');
		params.delete('account');
		const text = params.toString();
		router.go(text ? `/?${text}` : '/');
		setTimeout(() => document.querySelector<HTMLElement>(selector)?.focus());
	}

	/** The start button of one mailbox's panel: the same dialog as for a selection, with one mailbox. */
	function startOne(account: Account) {
		dialog = { kind: 'import', plan: importPlan([account], (id) => data.jobs.get(id), (id) => data.psts.get(id)) };
	}
</script>

<svelte:window onscroll={onScroll} onresize={measure} />

{#snippet settings(id: number)}
	<AccountSettings {id} ondetails={(accountId) => (dialog = { kind: 'details', id: accountId })} onupload={uploadFor} onroute={(a) => (dialog = { kind: 'route', account: a })} onedit={(a) => (dialog = { kind: 'edit', account: a })} onstart={startOne} />
{/snippet}

<!-- On a wide screen the list stays beside the panel instead of under it. -->
<div class={`transition-[padding] duration-200 ${panelJob != null || panelAccount != null ? 'min-[1680px]:pr-[600px]' : ''}`}>
<PageHeader title={m.migrations_page_title()} {sentence}>
	{#snippet actions()}
		{#if data.servers.length}
			<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })} data-testid="new-migration">{m.migrations_add_label()}</Button>
		{/if}
	{/snippet}
</PageHeader>

<div class="flex flex-col gap-4">
	{#if !data.workerAlive}
		<Notice>{m.migrations_page_worker_down()}</Notice>
	{/if}

	{#if data.error && !data.accounts.size}
		<ErrorNotice title={m.migrations_page_load_failed()} message={data.error} onretry={() => data.load()} />
	{:else if data.loading}
		<div class="flex flex-col gap-2" data-testid="skeleton">
			{#each [0, 1, 2, 3, 4, 5] as i (i)}<Skeleton class="h-[var(--row-h)]" />{/each}
		</div>
	{:else if !data.servers.length || !data.accounts.size}
		<GettingStarted hasServer={!!data.servers.length} onserver={() => (dialog = { kind: 'server' })} onnew={() => (dialog = { kind: 'add' })} oncsv={() => (dialog = { kind: 'csv' })} />
	{:else}
		<div data-testid="overall"><Progress value={overallPercent} label={m.migrations_page_overall()} /></div>

		<div class="flex flex-col gap-2">
			<div class="flex flex-wrap items-center gap-1.5">
				<div class="flex flex-wrap items-center gap-1.5" role="group" aria-label={m.migrations_filter_status()}>
					{#each CHIPS as chip (chip.value)}
						<button
							type="button"
							class={`inline-flex min-h-10 items-center gap-1.5 rounded-btn border px-3 font-medium transition-colors duration-200 ${status === chip.value ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
							aria-pressed={status === chip.value}
							onclick={() => router.setParams({ status: chip.value || null })}
							data-testid={`chip-${chip.value || 'all'}`}
						>
							{chip.label}
							<span class="font-mono text-ink-2">{chip.count}</span>
						</button>
					{/each}
				</div>
				<div class="flex flex-wrap items-center gap-1" role="group" aria-label={m.migrations_conn_group()} data-testid="conn-chips">
					{#each CONN_CHIPS as chip (chip.value)}
						{@const Icon = chip.icon}
						<button
							type="button"
							class={`inline-flex min-h-10 items-center gap-1 rounded-btn px-2 text-sm transition-colors duration-200 ${status === chip.value ? 'bg-surface-2 text-ink' : 'text-ink-2 hover:text-ink'}`}
							aria-pressed={status === chip.value}
							onclick={() => router.setParams({ status: status === chip.value ? null : chip.value })}
							data-testid={`conn-${chip.value}`}
						>
							<Icon size={14} class={chip.tone} />{chip.label}
						</button>
					{/each}
				</div>
				<Button variant="ghost" icon={SlidersHorizontal} aria-expanded={moreOpen} onclick={() => (moreOpen = !moreOpen)} data-testid="filter-more">{m.migrations_filter_more()}</Button>
				<label class="relative ml-auto min-w-40 flex-1 basis-40 sm:max-w-[260px]">
					<span class="sr-only">{m.migrations_filter_search()}</span>
					<span class="pointer-events-none absolute top-3 left-3 text-ink-3"><Search size={14} /></span>
					<input
						class="input pl-8"
						type="search"
						placeholder={m.migrations_filter_search()}
						value={router.params.get('q') ?? ''}
						oninput={(e) => router.setParams({ q: e.currentTarget.value || null })}
						data-testid="filter-q"
					/>
				</label>
			</div>
			{#if moreOpen}
				<div class="flex flex-wrap items-center gap-2" data-testid="filters-more">
					<select class="input w-auto max-w-48" aria-label={m.migrations_filter_status()} value={status} onchange={(e) => router.setParams({ status: e.currentTarget.value || null })} data-testid="filter-status">
						<option value="">{m.migrations_filter_all_status()}</option>
						<option value="untested">{m.migrations_filter_untested()}</option>
						<option value="tested_ok">{m.migrations_filter_tested_ok()}</option>
						<option value="test_failed">{m.migrations_filter_test_failed()}</option>
						<option value="no_pst">{m.migrations_filter_no_pst()}</option>
						<option value="active">{m.migrations_filter_active()}</option>
						<option value="done">{m.migrations_filter_done()}</option>
						<option value="problem">{m.migrations_filter_problem()}</option>
					</select>
					{#if data.servers.length > 1}
						<select class="input w-auto max-w-48" aria-label={m.migrations_filter_server()} value={server == null ? '' : String(server)} onchange={(e) => router.setParams({ server: e.currentTarget.value || null })}>
							<option value="">{m.migrations_filter_all_servers()}</option>
							{#each data.servers as item (item.id)}
								<option value={String(item.id)}>{item.name}</option>
							{/each}
						</select>
					{/if}
					{#if data.batches.length}
						<select class="input w-auto max-w-48" aria-label={m.batch_filter()} value={batch == null ? '' : String(batch)} onchange={(e) => router.setParams({ batch: e.currentTarget.value || null })} data-testid="filter-batch">
							<option value="">{m.batch_all()}</option>
							{#each data.batches as item (item.id)}
								<option value={String(item.id)}>{item.name}</option>
							{/each}
						</select>
					{/if}
				</div>
			{/if}
		</div>
		{#if batch}<BatchBar id={batch} />{/if}

		{#if !rows.length}
			<EmptyState icon={SearchX} title={m.migrations_empty_no_match_title()} description={m.migrations_empty_no_match_text({ total: all.length })}>
				{#snippet action()}
					<Button onclick={clearFilters}>{m.migrations_filter_clear()}</Button>
				{/snippet}
			</EmptyState>
		{:else}
			<Panel>
				<div class="row-head" data-testid="accounts-head">
					<label class="check">
						<input
							type="checkbox"
							checked={header === 'all'}
							indeterminate={header === 'some'}
							onchange={() => toggleAllVisible(header, visibleIds, data.selected)}
							data-testid="select-all"
						/>
						<span class="whitespace-nowrap">{m.migrations_table_select_all()}</span>
					</label>
					{#if hasFilter}
						<span class="px-2 text-xs" data-testid="filter-count">{m.migrations_filter_count({ shown: rows.length, total: all.length })}</span>
					{/if}
				</div>
				<ul bind:this={list} class="row-list" aria-label={m.migrations_list_label()} data-testid="accounts">
					{#if range.before}
						<li aria-hidden="true" style:height={`${range.before}px`}></li>
					{/if}
					{#each windowed as account (account.id)}
						<li class={`acct-row ${data.selected.has(account.id) ? 'is-selected' : ''}`} data-account-id={account.id}>
							<AccountRow
								id={account.id}
								onedit={(a) => (dialog = { kind: 'edit', account: a })}
								ondelete={(a) => (dialog = { kind: 'delete', account: a })}
								onroute={(a) => (dialog = { kind: 'route', account: a })}
							/>
						</li>
					{/each}
					{#if range.after}
						<li aria-hidden="true" style:height={`${range.after}px`}></li>
					{/if}
				</ul>
			</Panel>
		{/if}
	{/if}
</div>

</div>

{#if data.selected.size}
	<SelectionBar
		targets={split.targets.length}
		hidden={split.hidden.length}
		testing={testingCount}
		besidePanel={panelJob != null || panelAccount != null}
		ontest={testSelected}
		onimport={openImport}
		onclear={() => data.selected.clear()}
		ondropHidden={() => {
			for (const id of split.hidden) data.selected.delete(id);
		}}
	/>
{/if}

{#if panelJob != null || panelAccount != null}
	<aside
		class="fixed top-[var(--bar-h)] right-0 bottom-0 z-40 flex w-full max-w-[600px] flex-col border-l border-line-strong bg-surface shadow-float"
		aria-label={m.jobs_panel_label()}
		transition:slide={{ x: 64, duration: 240 }}
		data-testid="job-panel"
	>
		{#key `${panelJob}/${panelAccount}`}
			{#if panelJob != null}
				<JobPanel id={panelJob} onclose={closePanel} {settings} />
			{:else if panelAccount != null}
				<AccountPanel id={panelAccount} onclose={closePanel} {settings} />
			{/if}
		{/key}
	</aside>
{/if}

{#if dialog?.kind === 'server'}
	<ServerDialog onclose={closeDialog} />
{:else if dialog?.kind === 'add'}
	<AccountDialog defaultServer={server} onclose={closeDialog} oncsv={() => (dialog = { kind: 'csv' })} />
{:else if dialog?.kind === 'edit'}
	<AccountDialog account={dialog.account} onclose={closeDialog} />
{:else if dialog?.kind === 'csv'}
	<CsvDialog defaultServer={server} onclose={closeDialog} />
{:else if dialog?.kind === 'route'}
	<RouteDialog account={dialog.account} onclose={closeDialog} />
{:else if dialog?.kind === 'details'}
	<TestDialog accountId={dialog.id} onclose={closeDialog} />
{:else if dialog?.kind === 'import'}
	<ImportDialog plan={dialog.plan} onclose={closeDialog} ondone={imported} />
{:else if dialog?.kind === 'delete'}
	{@const target = dialog.account}
	<Confirm title={m.migrations_delete_title()} text={m.migrations_delete_text({ email: prefs.email(target.email) })} confirm={m.migrations_delete_confirm()} danger onconfirm={() => removeAccount(target)} onclose={closeDialog} />
{/if}
