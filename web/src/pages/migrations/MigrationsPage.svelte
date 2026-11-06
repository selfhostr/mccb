<script lang="ts">
	import FileUp from '@lucide/svelte/icons/file-up';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import SearchX from '@lucide/svelte/icons/search-x';
	import Server from '@lucide/svelte/icons/server';
	import Users from '@lucide/svelte/icons/users';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, del } from '../../lib/api/client.ts';
	import type { Account } from '../../lib/api/types.ts';
	import { importPlan, type ImportPlan, matchesFilter, type StatusFilter, STATUS_FILTERS, summarize } from '../../lib/logic/accounts.ts';
	import { headerState, splitSelection, toggleAllVisible } from '../../lib/logic/selection.ts';
	import { VIRTUAL_AT, windowRange } from '../../lib/logic/virtual.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import { rowFade, slide } from '../../lib/ui/motion.ts';
	import Notice from '../../lib/ui/Notice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import AccountDialog from './AccountDialog.svelte';
	import AccountRow from './AccountRow.svelte';
	import CsvDialog from './CsvDialog.svelte';
	import ImportDialog from './ImportDialog.svelte';
	import JobPanel from './JobPanel.svelte';
	import SelectionBar from './SelectionBar.svelte';
	import TestDialog from './TestDialog.svelte';

	const q = $derived((router.params.get('q') ?? '').trim().toLowerCase());
	const server = $derived(Number(router.params.get('server')) || null);
	const status = $derived.by<StatusFilter>(() => {
		const value = router.params.get('status') as StatusFilter | null;
		return value && STATUS_FILTERS.includes(value) ? value : '';
	});
	const hasFilter = $derived(!!(q || server || status));
	const needsJob = $derived(status === 'active' || status === 'done' || status === 'problem');

	const all = $derived([...data.accounts.values()].sort((a, b) => a.email.localeCompare(b.email)));
	const rows = $derived.by(() => {
		const filter = { q, server, status };
		// Without a status filter, job events must not run this: progress would refilter the list every frame.
		const jobOf = needsJob ? (id: number) => data.jobs.get(id) : () => undefined;
		return all.filter((a) => matchesFilter(a, jobOf(a.id), filter));
	});
	const visibleIds = $derived(rows.map((a) => a.id));
	const psts = $derived([...data.psts.values()].sort((a, b) => a.filename.localeCompare(b.filename)));
	const summary = $derived(summarize(all, (id) => data.jobs.get(id)));

	const header = $derived(headerState(visibleIds, data.selected));
	const split = $derived(splitSelection(data.selected, visibleIds));
	const testingCount = $derived(split.targets.filter((id) => data.testing.has(id)).length);

	// A long list renders only what is on screen, plus a margin. Row height is measured, not assumed.
	let tbody = $state<HTMLElement>();
	let offset = $state(0);
	let viewport = $state(typeof innerHeight === 'number' ? innerHeight : 800);
	let rowHeight = $state(52);
	const range = $derived(windowRange(rows.length, offset, viewport, rowHeight));
	const windowed = $derived(rows.slice(range.start, range.end));
	const virtual = $derived(rows.length > VIRTUAL_AT);

	let scrollPlanned = false;
	function measure() {
		scrollPlanned = false;
		if (!tbody) return;
		offset = -tbody.getBoundingClientRect().top;
		viewport = innerHeight;
		const row = tbody.querySelector<HTMLElement>('.acct-row');
		if (row && row.offsetHeight) rowHeight = row.offsetHeight;
	}
	function onScroll() {
		if (scrollPlanned || !virtual) return;
		scrollPlanned = true;
		requestAnimationFrame(measure);
	}
	$effect(() => {
		// Measure again when the list or the columns change shape.
		void rows.length;
		void prefs.value.columns;
		void prefs.value.density;
		requestAnimationFrame(measure);
	});

	const colCount = $derived(3 + Object.values(prefs.value.columns).filter(Boolean).length);

	let dialog = $state.raw<
		| { kind: 'add' }
		| { kind: 'csv' }
		| { kind: 'edit'; account: Account }
		| { kind: 'delete'; account: Account }
		| { kind: 'details'; id: number }
		| { kind: 'import'; plan: ImportPlan }
		| null
	>(null);
	const closeDialog = () => (dialog = null);

	function clearFilters() {
		router.setParams({ q: null, server: null, status: null });
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
		input.accept = '.pst,.ost';
		input.onchange = () => {
			const file = input.files?.[0];
			if (!file) return;
			uploads.add([file], 'size-asc', id);
			toast(m.migrations_row_upload_queued(), 'info');
		};
		input.click();
	}

	const panelOpen = $derived(router.route.jobId != null);
	function closePanel() {
		const id = router.route.jobId;
		const params = new URLSearchParams(location.search);
		params.delete('tab');
		const text = params.toString();
		router.go(text ? `/?${text}` : '/');
		setTimeout(() => document.querySelector<HTMLElement>(`[data-job-link="${id}"]`)?.focus());
	}

	const addItems = $derived([
		{ label: m.migrations_add_account(), icon: Plus, onselect: () => (dialog = { kind: 'add' }) },
		{ label: m.migrations_add_csv(), icon: FileUp, onselect: () => (dialog = { kind: 'csv' }) }
	]);
</script>

<svelte:window onscroll={onScroll} onresize={measure} />

<PageHeader title={m.migrations_page_title()} sentence={data.loading ? m.common_loading() : m.migrations_page_summary(summary)}>
	{#snippet actions()}
		{#if data.servers.length}
			<Menu label={m.migrations_add_label()} icon={Plus} text={m.migrations_add_label()} items={addItems} />
		{/if}
	{/snippet}
</PageHeader>

{#if !data.workerAlive}
	<div class="mb-3"><Notice>{m.migrations_page_worker_down()}</Notice></div>
{/if}

{#if data.error && !data.accounts.size}
	<ErrorNotice title={m.migrations_page_load_failed()} message={data.error} onretry={() => data.load()} />
{:else if data.loading}
	<div class="flex flex-col gap-2" data-testid="skeleton">
		{#each [0, 1, 2, 3, 4, 5] as i (i)}<Skeleton class="h-[var(--row-h)]" />{/each}
	</div>
{:else if !data.servers.length}
	<EmptyState icon={Server} title={m.migrations_empty_no_server_title()} description={m.migrations_empty_no_server_text()}>
		{#snippet action()}
			<Button variant="primary" href="/settings#servers">{m.migrations_empty_no_server_action()}</Button>
		{/snippet}
	</EmptyState>
{:else if !data.accounts.size}
	<EmptyState icon={Users} title={m.migrations_empty_no_account_title()} description={m.migrations_empty_no_account_text()}>
		{#snippet action()}
			<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })}>{m.migrations_add_account()}</Button>
			<Button icon={FileUp} onclick={() => (dialog = { kind: 'csv' })}>{m.migrations_add_csv()}</Button>
		{/snippet}
	</EmptyState>
{:else}
	<div class="mb-2 flex flex-wrap items-center gap-2">
		<label class="relative min-w-40 flex-1 basis-56">
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
		{#if data.servers.length > 1}
			<select class="input w-auto max-w-48" aria-label={m.migrations_filter_server()} value={server == null ? '' : String(server)} onchange={(e) => router.setParams({ server: e.currentTarget.value || null })}>
				<option value="">{m.migrations_filter_all_servers()}</option>
				{#each data.servers as item (item.id)}
					<option value={String(item.id)}>{item.name}</option>
				{/each}
			</select>
		{/if}
		<select class="input w-auto max-w-48" aria-label={m.migrations_filter_status()} value={status} onchange={(e) => router.setParams({ status: e.currentTarget.value || null })} data-testid="filter-status">
			<option value="">{m.migrations_filter_all_status()}</option>
			<option value="untested">{m.migrations_filter_untested()}</option>
			<option value="test_failed">{m.migrations_filter_test_failed()}</option>
			<option value="no_pst">{m.migrations_filter_no_pst()}</option>
			<option value="active">{m.migrations_filter_active()}</option>
			<option value="done">{m.migrations_filter_done()}</option>
			<option value="problem">{m.migrations_filter_problem()}</option>
		</select>
	</div>

	{#if !rows.length}
		<EmptyState icon={SearchX} title={m.migrations_empty_no_match_title()} description={m.migrations_empty_no_match_text({ total: all.length })}>
			{#snippet action()}
				<Button onclick={clearFilters}>{m.migrations_filter_clear()}</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<table class="acct-table" data-testid="accounts">
			<thead>
				<tr>
					<th class="sel w-11" scope="col">
						<label class="check">
							<input
								type="checkbox"
								checked={header === 'all'}
								indeterminate={header === 'some'}
								aria-label={m.migrations_table_select_all()}
								onchange={() => toggleAllVisible(header, visibleIds, data.selected)}
								data-testid="select-all"
							/>
							<span class="min-[1100px]:sr-only">{m.migrations_table_select_all()}</span>
						</label>
					</th>
					<th scope="col">{m.migrations_col_account()}</th>
					{#if prefs.value.columns.server}<th class="w-28" scope="col">{m.migrations_col_server()}</th>{/if}
					{#if prefs.value.columns.connection}<th class="w-48" scope="col">{m.migrations_col_connection()}</th>{/if}
					{#if prefs.value.columns.pst}<th class="w-44" scope="col">{m.migrations_col_pst()}</th>{/if}
					{#if prefs.value.columns.content}<th class="w-[150px]" scope="col">{m.migrations_col_content()}</th>{/if}
					{#if prefs.value.columns.progress}<th class="w-48" scope="col">{m.migrations_col_progress()}</th>{/if}
					<th class="w-24" scope="col"><span class="sr-only">{m.migrations_col_actions()}</span></th>
				</tr>
			</thead>
			<tbody bind:this={tbody}>
				{#if range.before}
					<tr class="acct-spacer" aria-hidden="true" style:height={`${range.before}px`}><td colspan={colCount}></td></tr>
				{/if}
				{#each windowed as account (account.id)}
					<tr class={`acct-row ${data.selected.has(account.id) ? 'is-selected' : ''}`} data-account-id={account.id} transition:rowFade={{ duration: virtual ? 0 : 140 }}>
						<AccountRow
							id={account.id}
							{psts}
							onedit={(a) => (dialog = { kind: 'edit', account: a })}
							ondelete={(a) => (dialog = { kind: 'delete', account: a })}
							ondetails={(a) => (dialog = { kind: 'details', id: a.id })}
							onupload={uploadFor}
						/>
					</tr>
				{/each}
				{#if range.after}
					<tr class="acct-spacer" aria-hidden="true" style:height={`${range.after}px`}><td colspan={colCount}></td></tr>
				{/if}
			</tbody>
		</table>
		{#if hasFilter}
			<p class="mt-2 text-ink-3" data-testid="filter-count">{m.migrations_filter_count({ shown: rows.length, total: all.length })}</p>
		{/if}
	{/if}
{/if}

{#if data.selected.size}
	<SelectionBar
		targets={split.targets.length}
		hidden={split.hidden.length}
		testing={testingCount}
		ontest={testSelected}
		onimport={openImport}
		onclear={() => data.selected.clear()}
		ondropHidden={() => {
			for (const id of split.hidden) data.selected.delete(id);
		}}
	/>
{/if}

{#if panelOpen}
	<aside
		class="fixed top-[var(--bar-h)] right-0 bottom-[var(--tab-h)] z-40 flex w-full max-w-[600px] flex-col border-l border-line-strong bg-surface shadow-float sm:bottom-0"
		aria-label={m.jobs_panel_label()}
		transition:slide={{ x: 64, duration: 240 }}
		data-testid="job-panel"
	>
		{#key router.route.jobId}
			<JobPanel id={router.route.jobId as number} onclose={closePanel} />
		{/key}
	</aside>
{/if}

{#if dialog?.kind === 'add'}
	<AccountDialog defaultServer={server} onclose={closeDialog} />
{:else if dialog?.kind === 'edit'}
	<AccountDialog account={dialog.account} onclose={closeDialog} />
{:else if dialog?.kind === 'csv'}
	<CsvDialog defaultServer={server} onclose={closeDialog} />
{:else if dialog?.kind === 'details'}
	<TestDialog accountId={dialog.id} onclose={closeDialog} />
{:else if dialog?.kind === 'import'}
	<ImportDialog plan={dialog.plan} onclose={closeDialog} ondone={imported} />
{:else if dialog?.kind === 'delete'}
	{@const target = dialog.account}
	<Confirm title={m.migrations_delete_title()} text={m.migrations_delete_text({ email: prefs.email(target.email) })} confirm={m.migrations_delete_confirm()} danger onconfirm={() => removeAccount(target)} onclose={closeDialog} />
{/if}
