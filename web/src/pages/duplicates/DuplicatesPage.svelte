<script lang="ts">
	/**
	 * Duplicates tab: the migration mailboxes and the mailboxes added by hand (one list shared with Repair, no
	 * migration needed). Analyse first (writes nothing), read the counts, then remove: the deletion is permanent,
	 * one copy of each is always kept.
	 */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import Copy from '@lucide/svelte/icons/copy';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import CopyX from '@lucide/svelte/icons/copy-x';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FileUp from '@lucide/svelte/icons/file-up';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import KeyRound from '@lucide/svelte/icons/key-round';
	import Plus from '@lucide/svelte/icons/plus';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import Server from '@lucide/svelte/icons/server';
	import Square from '@lucide/svelte/icons/square';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import type { DedupBox, DedupKind, DedupMode } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import { clock } from '../../lib/ui/clock.svelte.ts';
	import { connectionCounts, connectionOf } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { dedupLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { duplicates } from '../../lib/stores/duplicates.svelte.ts';
	import { repairs } from '../../lib/stores/repair.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import MaintenanceTabs from '../MaintenanceTabs.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import RepairBoxDialog from '../repair/RepairBoxDialog.svelte';
	import RepairCsvDialog from '../repair/RepairCsvDialog.svelte';
	import DuplicatesDetail from './DuplicatesDetail.svelte';
	import { KINDS, kindCount, kindLabel, planRemoval } from './helpers.ts';

	onMount(() => duplicates.follow());

	let dialog = $state.raw<{ kind: 'add' } | { kind: 'edit'; box: DedupBox } | { kind: 'csv' } | null>(null);
	let removing = $state.raw<DedupBox | null>(null);
	let confirming = $state.raw<string[] | null>(null);
	let confirmBusy = $state(false);
	let open = $state<string | null>(null);
	/** A failed start or removal stays on the page until the next action. */
	let actionError = $state<{ title: string; message: string } | null>(null);

	const boxes = $derived(duplicates.boxes);
	const kinds = $derived(KINDS.filter((k) => duplicates.kinds.has(k)));
	const conn = $derived(connectionCounts(boxes));
	const selected = $derived(boxes.filter((b) => duplicates.selected.has(b.key)));
	const allSelected = $derived(boxes.length > 0 && selected.length === boxes.length);
	const openBox = $derived(boxes.find((b) => b.key === open) ?? null);
	const serverName = (id: number) => data.servers.find((s) => s.id === id)?.name ?? '';
	/** A box can start a run when nothing else writes to it and it has a password. */
	const free = (b: DedupBox) => b.usable && !b.busy && !b.active_run;

	const plan = $derived(confirming ? planRemoval(boxes.filter((b) => confirming!.includes(b.key)), kinds) : null);
	const removable = $derived(selected.some((b) => reason(b, true) === undefined));
	const stale = $derived(plan?.oldest != null && clock.now - plan.oldest > 24 * 3600 * 1000);
	const testable = $derived(selected.filter((b) => byHand(b)));

	/** Why Analyse or Remove is off for this mailbox, in words. */
	function reason(box: DedupBox, removal: boolean): string | undefined {
		if (box.busy) return box.busy === 'migration' ? m.dedup_row_busy_migration() : m.dedup_row_busy_repair();
		if (!box.usable) return m.dedup_row_no_password();
		if (box.active_run) return m.dedup_reason_busy();
		if (removal && !box.last_scan) return m.dedup_reason_scan_first();
		if (removal && !box.last_scan!.progress.duplicates) return m.dedup_reason_none();
		return undefined;
	}

	/** The step to offer on the row: remove once an analysis found duplicates, else analyse. Nothing while it cannot run. */
	function nextAction(box: DedupBox): 'scan' | 'remove' | null {
		if (!free(box)) return null;
		return box.last_scan && box.last_scan.progress.duplicates > 0 ? 'remove' : 'scan';
	}

	/** A mailbox added by hand (not a migration): it can be tested, edited and removed from the list here. */
	const byHand = (b: DedupBox) => b.source === 'repair';
	const testing = (b: DedupBox) => byHand(b) && repairs.testing.has(b.ref_id);
	const changed = () => void duplicates.refresh();

	function connLook(box: DedupBox) {
		if (testing(box)) return { icon: FlaskConical, tone: 'text-info-ink', label: m.migrations_test_running() };
		const c = connectionOf(box);
		if (c === 'ok') return { icon: CircleCheck, tone: 'text-signal-ink', label: m.migrations_test_status_ok() };
		if (c === 'untested') return { icon: CircleDashed, tone: 'text-ink-3', label: m.migrations_test_never() };
		return { icon: CircleX, tone: 'text-warning-ink', label: box.status === 'auth_failed' ? m.migrations_row_login_refused() : m.migrations_test_status_failed() };
	}

	/** One line under the address: why it cannot run, else progress while it runs, else what the last run found or did. */
	function resultLine(box: DedupBox): string {
		if (box.busy) return box.busy === 'migration' ? m.dedup_row_busy_migration() : m.dedup_row_busy_repair();
		if (box.unusable_reason === 'no_password' || !box.usable) return m.dedup_row_no_password();
		if (box.active_run) {
			const p = box.active_run.progress;
			return m.dedup_row_progress({ seen: fmtNum(p.seen), total: fmtNum(p.total), folder: p.folder ?? '' });
		}
		const run = box.last_run ?? box.last_scan;
		if (!run) return '';
		const p = run.progress;
		if (run.status === 'failed') return run.error ?? '';
		if (run.mode === 'dedup') return m.dedup_row_removed({ count: fmtNum(p.deleted) });
		const items = Object.values(run.totals).reduce((n, t) => n + (t?.items ?? 0), 0);
		return p.duplicates ? m.dedup_row_found({ count: fmtNum(p.duplicates), groups: fmtNum(p.groups) }) : m.dedup_row_none({ items: fmtNum(items) });
	}

	function percent(box: DedupBox): number {
		const p = box.active_run?.progress;
		return p && p.total ? Math.min(100, (p.seen / p.total) * 100) : 0;
	}

	async function run(keys: string[], mode: DedupMode) {
		if (!keys.length) return;
		actionError = null;
		try {
			const res = await duplicates.start(keys, mode, kinds);
			toast(m.dedup_started({ count: res.runs.length }), 'signal');
			if (res.skipped_busy.length) toast(m.dedup_skipped_busy({ count: res.skipped_busy.length }), 'warning');
		} catch (err) {
			actionError = { title: m.dedup_run_failed(), message: errorText(err) };
		}
	}

	async function stop(box: DedupBox) {
		actionError = null;
		try {
			await duplicates.stop(box.active_run!.id);
		} catch (err) {
			actionError = { title: m.dedup_run_failed(), message: errorText(err) };
		}
	}

	function ask(keys: string[]) {
		const p = planRemoval(boxes.filter((b) => keys.includes(b.key)), kinds);
		if (!p.keys.length) {
			toast(p.unanalysed ? m.dedup_confirm_unanalysed({ count: p.unanalysed }) : m.dedup_nothing_to_remove(), 'warning');
			return;
		}
		confirming = keys;
	}

	async function confirmRemoval() {
		if (!plan) return;
		confirmBusy = true;
		try {
			await run(plan.keys, 'dedup');
		} finally {
			confirmBusy = false;
			confirming = null;
		}
	}

	async function test(box: DedupBox) {
		await repairs.test([box.ref_id]);
		await duplicates.refresh();
	}

	async function forget(box: DedupBox) {
		actionError = null;
		try {
			await repairs.remove(box.ref_id);
			if (open === box.key) open = null;
			await duplicates.refresh();
			toast(m.repair_box_deleted({ email: prefs.email(box.email) }), 'signal');
		} catch (err) {
			actionError = { title: m.repair_remove_failed(), message: errorText(err) };
		}
	}

	function toggleAll(e: Event & { currentTarget: HTMLInputElement }) {
		if (e.currentTarget.checked) for (const b of boxes) duplicates.selected.add(b.key);
		else duplicates.selected.clear();
	}

	async function testSelected() {
		await repairs.test(testable.map((b) => b.ref_id));
		await duplicates.refresh();
	}

	function toggleKind(kind: DedupKind, on: boolean) {
		if (on) duplicates.kinds.add(kind);
		else if (duplicates.kinds.size > 1) duplicates.kinds.delete(kind);
	}

	function menu(box: DedupBox) {
		const active = !!box.active_run;
		const can = free(box);
		return [
			{ label: m.dedup_action_scan(), icon: ScanSearch, disabled: reason(box, false) !== undefined, reason: reason(box, false), onselect: () => run([box.key], 'scan') },
			{ label: m.dedup_action_remove(), icon: CopyX, danger: true, disabled: reason(box, true) !== undefined, reason: reason(box, true), onselect: () => ask([box.key]) },
			...(active && box.active_run?.status !== 'stopping' ? [{ label: m.dedup_action_stop(), icon: Square, onselect: () => stop(box) }] : []),
			...(byHand(box)
				? [
						{ label: m.migrations_row_test(), icon: FlaskConical, disabled: testing(box), onselect: () => test(box) },
						{ label: m.migrations_row_edit(), icon: KeyRound, disabled: active, onselect: () => (dialog = { kind: 'edit', box }) },
						{ label: m.repair_action_forget(), icon: Trash2, disabled: active, onselect: () => (removing = box) }
					]
				: [])
		];
	}
</script>

<div id="duplicates-panel" class="flex flex-col gap-5" data-testid="dedup-page">
	<PageHeader title={m.shell_nav_maintenance()}>
		{#snippet actions()}
			{#if data.servers.length && boxes.length}
				<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })} data-testid="dedup-add">{m.repair_add()}</Button>
			{/if}
		{/snippet}
	</PageHeader>

	<MaintenanceTabs />

	<Notice tone="note">
		<p class="m-0">{m.dedup_notice()}</p>
		<Disclosure title={m.dedup_how_title()}>
			<ul class="m-0 flex list-none flex-col gap-1 p-0 text-ink-2">
				<li>{m.dedup_how_mail()}</li>
				<li>{m.dedup_how_calendar()}</li>
				<li>{m.dedup_how_contacts()}</li>
				<li>{m.dedup_how_scope()}</li>
			</ul>
		</Disclosure>
	</Notice>

	{#if actionError}<ErrorNotice title={actionError.title} message={actionError.message} />{/if}

	{#if duplicates.error && !duplicates.loaded}
		<ErrorNotice title={m.dedup_load_failed()} message={duplicates.error} />
	{:else if duplicates.loaded && !boxes.length && !data.servers.length && !data.loading}
		<EmptyState icon={Server} title={m.migrations_empty_no_server_title()} description={m.repair_no_server_text()}>
			{#snippet action()}<Button variant="primary" href="/servers">{m.migrations_empty_no_server_action()}</Button>{/snippet}
		</EmptyState>
	{:else if duplicates.loaded && !boxes.length}
		<EmptyState icon={Copy} title={m.dedup_empty_title()} description={m.dedup_empty_text()}>
			{#snippet action()}
				<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })} data-testid="dedup-add">{m.repair_add()}</Button>
				<Button icon={FileUp} onclick={() => (dialog = { kind: 'csv' })}>{m.migrations_add_csv()}</Button>
			{/snippet}
		</EmptyState>
	{:else if boxes.length}
		<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
			<fieldset class="m-0 flex min-w-0 flex-wrap items-center gap-x-4 border-0 p-0" data-testid="dedup-kinds">
				<legend class="sr-only">{m.dedup_kinds_label()}</legend>
				<span class="text-sm text-ink-2" aria-hidden="true">{m.dedup_kinds_label()}</span>
				{#each KINDS as kind (kind)}
					<label class="check min-h-10 gap-2 text-sm">
						<input type="checkbox" checked={duplicates.kinds.has(kind)} disabled={duplicates.kinds.size === 1 && duplicates.kinds.has(kind)} onchange={(e) => toggleKind(kind, e.currentTarget.checked)} data-testid={`dedup-kind-${kind}`} />
						{kindLabel(kind)}
					</label>
				{/each}
			</fieldset>
			<div class="flex flex-wrap items-center gap-1" role="group" aria-label={m.migrations_conn_group()} data-testid="dedup-conn">
				<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleCheck size={14} class="text-signal-ink" />{m.migrations_conn_ok({ count: conn.ok })}</span>
				<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleX size={14} class="text-warning-ink" />{m.migrations_conn_failed({ count: conn.failed })}</span>
				<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleDashed size={14} class="text-ink-3" />{m.migrations_conn_untested({ count: conn.untested })}</span>
			</div>
		</div>

		<Panel>
			<div class="flex items-center gap-3 border-b border-line px-4 py-2">
				<label class="check justify-start gap-2">
					<input type="checkbox" checked={allSelected} indeterminate={selected.length > 0 && !allSelected} onchange={toggleAll} />
					<span class="text-sm text-ink-2">{m.dedup_select_all()}</span>
				</label>
			</div>
			<ul class="stagger m-0 flex list-none flex-col divide-y divide-line p-0" data-testid="dedup-list">
				{#each boxes as box (box.key)}
					{@const look = dedupLook(box)}
					{@const c = connLook(box)}
					{@const Icon = c.icon}
					{@const email = prefs.email(box.email)}
					{@const next = nextAction(box)}
					<li class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2" data-dedup-box={box.email}>
						<label class="check">
							<input type="checkbox" checked={duplicates.selected.has(box.key)} aria-label={m.migrations_row_select({ email })} onchange={(e) => (e.currentTarget.checked ? duplicates.selected.add(box.key) : duplicates.selected.delete(box.key))} />
						</label>
						<button type="button" class="flex min-h-10 min-w-0 flex-1 basis-56 items-center gap-2 rounded-btn text-left hover:bg-ghost" onclick={() => (open = open === box.key ? null : box.key)} aria-expanded={open === box.key} title={m.repair_row_expand({ email })} data-testid="dedup-open">
							<ChevronRight size={16} aria-hidden="true" class={`shrink-0 text-ink-3 transition-transform duration-200 rtl:rotate-180 ${open === box.key ? 'rotate-90 rtl:rotate-90' : ''}`} />
							<span class="flex min-w-0 flex-col items-start justify-center">
								<span class="flex min-w-0 items-center gap-1.5">
									<span class={`inline-flex shrink-0 ${c.tone}`} title={c.label} data-testid="dedup-conn-state" data-conn={testing(box) ? 'testing' : connectionOf(box)}>
										<Icon size={14} aria-hidden="true" /><span class="sr-only">{c.label}</span>
									</span>
									<span class="truncate font-medium">{email}</span>
								</span>
								<span class="flex max-w-full gap-2 text-xs text-ink-2" data-testid="dedup-line"><span class="truncate">{serverName(box.server_id)}</span>{#if resultLine(box)}<span class="truncate text-ink-3">{resultLine(box)}</span>{/if}</span>
							</span>
						</button>
						{#if !box.usable}
							<a class="text-sm" href="/migrations" data-testid="dedup-no-password">{m.dedup_row_no_password_link()}</a>
						{/if}
						<span data-testid="dedup-state"><Plate tone={look.tone} icon={look.icon} label={look.label} busy={look.busy} /></span>
						{#if box.active_run}
							<span class="w-32 shrink-0"><Progress value={percent(box)} label={m.dedup_row_progress_label({ email })} /></span>
						{:else if next === 'remove'}
							<Button variant="danger" icon={CopyX} onclick={() => ask([box.key])} data-testid="dedup-next">{m.dedup_action_remove()}</Button>
						{:else if next === 'scan'}
							<Button icon={ScanSearch} onclick={() => run([box.key], 'scan')} data-testid="dedup-next">{m.dedup_action_scan()}</Button>
						{/if}
						<Menu label={m.migrations_row_more({ email })} icon={EllipsisVertical} items={menu(box)} />
					</li>
					{#if open === box.key && openBox}
						<li class="bg-surface-2/40 px-4 py-3" data-testid="dedup-detail">
							<DuplicatesDetail box={openBox} onremove={() => ask([box.key])} />
						</li>
					{/if}
				{/each}
			</ul>
		</Panel>

		{#if selected.length}
			<div class="sticky bottom-3 z-30 flex flex-wrap items-center gap-2 rounded-card border border-line-strong bg-surface-2 p-2 shadow-float" data-testid="dedup-bulk">
				<span class="px-2 text-sm">{m.migrations_bar_selected({ count: selected.length })}</span>
				<Button icon={ScanSearch} onclick={() => run(selected.filter(free).map((b) => b.key), 'scan')} data-testid="bulk-scan">{m.dedup_action_scan()}</Button>
				<Button variant="danger" icon={CopyX} disabled={!removable} title={removable ? undefined : m.dedup_reason_scan_first()} onclick={() => ask(selected.map((b) => b.key))} data-testid="bulk-remove">{m.dedup_action_remove()}</Button>
				<Button variant="ghost" icon={FlaskConical} disabled={!testable.length} title={testable.length ? undefined : m.dedup_test_none()} onclick={testSelected} data-testid="bulk-test">{m.migrations_row_test()}</Button>
				<Button variant="ghost" onclick={() => duplicates.selected.clear()}>{m.dedup_selection_clear()}</Button>
			</div>
		{/if}
	{/if}
</div>

{#if dialog?.kind === 'add'}
	<RepairBoxDialog oncsv={() => (dialog = { kind: 'csv' })} onchange={changed} onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'edit'}
	{@const b = dialog.box}
	<RepairBoxDialog box={{ id: b.ref_id, server_id: b.server_id, email: b.email, login: b.login }} onchange={changed} onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'csv'}
	<RepairCsvDialog onchange={changed} onclose={() => (dialog = null)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.repair_delete_title()} text={m.repair_delete_text({ email: prefs.email(target.email) })} confirm={m.repair_action_forget_confirm()} onconfirm={() => forget(target)} onclose={() => (removing = null)} />
{/if}
{#if confirming && plan}
	<Dialog title={m.dedup_confirm_title()} onclose={() => (confirming = null)} center>
		<div class="flex flex-col gap-3" data-testid="dedup-confirm">
			<p class="m-0 font-semibold" data-testid="dedup-confirm-counts">{m.dedup_confirm_summary({ counts: kinds.map((k) => kindCount(k, plan.counts[k])).join(', '), boxes: plan.keys.length })}</p>
			<p class="m-0 font-semibold text-warning-ink">{m.dedup_confirm_permanent()}</p>
			<p class="m-0">{m.dedup_confirm_keep()}</p>
			{#if plan.oldest != null}<p class="m-0 text-sm text-ink-2">{m.dedup_confirm_scanned()} <RelTime value={plan.oldest} /></p>{/if}
			{#if stale}<Notice>{m.dedup_confirm_stale()}</Notice>{/if}
			<div class="flex flex-col gap-1">
				<p class="m-0 text-sm font-medium">{m.dedup_confirm_boxes()}</p>
				<ul class="m-0 flex max-h-48 list-none flex-col gap-1 overflow-y-auto p-0 text-sm" data-testid="dedup-confirm-boxes">
					{#each plan.boxes as b (b.key)}
						<li class="flex min-w-0 flex-col">
							<span class="truncate font-medium">{prefs.email(b.email)}</span>
							<span class="text-xs text-ink-2">{kinds.filter((k) => b.counts[k] > 0).map((k) => kindCount(k, b.counts[k])).join(', ')}</span>
						</li>
					{/each}
				</ul>
			</div>
			{#if plan.unanalysed}<p class="m-0 text-ink-2">{m.dedup_confirm_unanalysed({ count: plan.unanalysed })}</p>{/if}
		</div>
		{#snippet actions()}
			<Button variant="ghost" onclick={() => (confirming = null)}>{m.common_cancel()}</Button>
			<Button variant="danger" loading={confirmBusy} onclick={confirmRemoval} data-testid="dedup-confirm-go">{m.dedup_action_remove()}</Button>
		{/snippet}
	</Dialog>
{/if}
