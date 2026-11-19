<script lang="ts">
	/**
	 * Repair tab: mailboxes damaged by another migration tool. Analyse first (writes nothing), read the result,
	 * then repair; every original is kept in the mailbox and can be put back.
	 */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FileUp from '@lucide/svelte/icons/file-up';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import KeyRound from '@lucide/svelte/icons/key-round';
	import Plus from '@lucide/svelte/icons/plus';
	import ScanSearch from '@lucide/svelte/icons/scan-search';
	import Server from '@lucide/svelte/icons/server';
	import Square from '@lucide/svelte/icons/square';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import Undo2 from '@lucide/svelte/icons/undo-2';
	import Wrench from '@lucide/svelte/icons/wrench';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import type { RepairBox, RepairMode } from '../../lib/api/types.ts';
	import { connectionCounts, connectionOf } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { repairLook } from '../../lib/status.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { repairs } from '../../lib/stores/repair.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import MaintenanceTabs from '../MaintenanceTabs.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import RepairBoxDialog from './RepairBoxDialog.svelte';
	import RepairConfirmDialog from './RepairConfirmDialog.svelte';
	import RepairCsvDialog from './RepairCsvDialog.svelte';
	import RepairDetail from './RepairDetail.svelte';

	onMount(() => repairs.follow());

	let dialog = $state.raw<{ kind: 'add' } | { kind: 'edit'; box: RepairBox } | { kind: 'csv' } | null>(null);
	let confirming = $state.raw<{ mode: 'repair' | 'restore'; ids: number[] } | null>(null);
	let removing = $state.raw<RepairBox | null>(null);
	let open = $state<number | null>(null);
	/** A failed start or removal stays on the page until the next action. */
	let actionError = $state<{ title: string; message: string } | null>(null);

	const boxes = $derived(repairs.boxes);
	const conn = $derived(connectionCounts(boxes));
	const selectedIds = $derived(boxes.filter((b) => repairs.selected.has(b.id)).map((b) => b.id));
	const allSelected = $derived(boxes.length > 0 && selectedIds.length === boxes.length);
	const repairIds = $derived(boxes.filter((b) => repairs.selected.has(b.id) && repairable(b)).map((b) => b.id));
	const openBox = $derived(boxes.find((b) => b.id === open) ?? null);
	const serverName = (id: number) => data.servers.find((s) => s.id === id)?.name ?? '';

	/** Repair needs an analysis that found damage, and nothing running on the mailbox. */
	function repairable(box: RepairBox): boolean {
		const last = box.last_scan;
		return !box.active_run && last?.mode === 'scan' && last.status !== 'failed' && last.status !== 'stopped' && (last.progress.affected ?? 0) > 0;
	}

	/** Why Repair is off for this mailbox. */
	function repairReason(box: RepairBox): string {
		if (box.active_run) return m.repair_reason_busy();
		const last = box.last_scan;
		return last?.mode === 'scan' && last.status !== 'failed' && last.status !== 'stopped' ? m.repair_reason_none_found() : m.repair_reason_scan_first();
	}

	function connLook(box: RepairBox) {
		if (repairs.testing.has(box.id)) return { icon: FlaskConical, tone: 'text-info-ink', label: m.migrations_test_running() };
		const c = connectionOf(box);
		if (c === 'ok') return { icon: CircleCheck, tone: 'text-signal-ink', label: m.migrations_test_status_ok() };
		if (c === 'untested') return { icon: CircleDashed, tone: 'text-ink-3', label: m.migrations_test_never() };
		return { icon: CircleX, tone: 'text-warning-ink', label: box.status === 'auth_failed' ? m.migrations_row_login_refused() : m.migrations_test_status_failed() };
	}

	/** One line under the address: progress while it runs, else what the last run found or did. */
	function resultLine(box: RepairBox): string {
		const run = box.active_run ?? box.last_scan;
		if (!run) return '';
		const p = run.progress;
		if (box.active_run) return m.repair_row_progress({ seen: p.seen, total: p.total, folder: p.folder ?? '' });
		if (run.status === 'failed') return run.error ?? '';
		if (run.mode === 'restore') return m.repair_row_restored({ count: p.restored });
		if (run.mode === 'repair') return m.repair_row_repaired({ count: p.repaired, seen: p.seen });
		return p.affected ? m.repair_row_damaged({ count: p.affected, seen: p.seen }) : m.repair_row_intact({ seen: p.seen });
	}

	function percent(box: RepairBox): number {
		const p = box.active_run?.progress;
		return p && p.total ? Math.min(100, (p.seen / p.total) * 100) : 0;
	}

	async function run(ids: number[], mode: RepairMode) {
		if (!ids.length) return;
		actionError = null;
		try {
			const res = await repairs.start(ids, mode);
			toast(m.repair_started({ count: res.runs.length }), 'signal');
			if (res.skipped_busy.length) toast(m.repair_skipped_busy({ count: res.skipped_busy.length }), 'warning');
		} catch (err) {
			actionError = { title: m.repair_run_failed(), message: errorText(err) };
		}
	}

	async function stop(box: RepairBox) {
		actionError = null;
		try {
			await repairs.stop(box.active_run!.id);
		} catch (err) {
			actionError = { title: m.repair_run_failed(), message: errorText(err) };
		}
	}

	async function remove(box: RepairBox) {
		actionError = null;
		try {
			await repairs.remove(box.id);
			if (open === box.id) open = null;
			toast(m.repair_box_deleted({ email: prefs.email(box.email) }), 'signal');
		} catch (err) {
			actionError = { title: m.repair_remove_failed(), message: errorText(err) };
		}
	}

	function toggleAll(e: Event & { currentTarget: HTMLInputElement }) {
		if (e.currentTarget.checked) for (const b of boxes) repairs.selected.add(b.id);
		else repairs.selected.clear();
	}

	function menu(box: RepairBox) {
		const busy = !!box.active_run;
		return [
			{ label: m.repair_action_scan(), icon: ScanSearch, disabled: busy, reason: busy ? m.repair_reason_busy() : undefined, onselect: () => run([box.id], 'scan') },
			{ label: m.repair_action_repair(), icon: Wrench, disabled: !repairable(box), reason: repairable(box) ? undefined : repairReason(box), onselect: () => (confirming = { mode: 'repair', ids: [box.id] }) },
			{ label: m.repair_action_restore(), icon: Undo2, disabled: busy, reason: busy ? m.repair_reason_busy() : undefined, onselect: () => (confirming = { mode: 'restore', ids: [box.id] }) },
			...(busy && box.active_run?.status !== 'stopping' ? [{ label: m.repair_action_stop(), icon: Square, onselect: () => stop(box) }] : []),
			{ label: m.migrations_row_test(), icon: FlaskConical, disabled: repairs.testing.has(box.id), onselect: () => repairs.test([box.id]) },
			{ label: m.migrations_row_edit(), icon: KeyRound, disabled: busy, onselect: () => (dialog = { kind: 'edit', box }) },
			{ label: m.repair_action_forget(), icon: Trash2, disabled: busy, onselect: () => (removing = box) }
		];
	}
</script>

<div id="repair-panel" class="flex flex-col gap-5" data-testid="repair-page">
	<PageHeader title={m.shell_nav_maintenance()}>
		{#snippet actions()}
			{#if data.servers.length && boxes.length}
				<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })} data-testid="repair-add">{m.repair_add()}</Button>
			{/if}
		{/snippet}
	</PageHeader>

	<MaintenanceTabs />

	<Notice tone="note">
		<p class="m-0">{m.repair_notice()}</p>
		<Disclosure title={m.repair_how_title()}><p class="m-0 text-ink-2">{m.repair_how_text()}</p></Disclosure>
	</Notice>

	{#if actionError}<ErrorNotice title={actionError.title} message={actionError.message} />{/if}

	{#if repairs.error && !repairs.loaded}
		<ErrorNotice title={m.repair_load_failed()} message={repairs.error} />
	{:else if !data.servers.length && !data.loading}
		<EmptyState icon={Server} title={m.migrations_empty_no_server_title()} description={m.repair_no_server_text()}>
			{#snippet action()}<Button variant="primary" href="/servers">{m.migrations_empty_no_server_action()}</Button>{/snippet}
		</EmptyState>
	{:else if repairs.loaded && !boxes.length}
		<EmptyState icon={Wrench} title={m.repair_empty_title()} description={m.repair_empty_text()}>
			{#snippet action()}
				<Button variant="primary" icon={Plus} onclick={() => (dialog = { kind: 'add' })} data-testid="repair-add">{m.repair_add()}</Button>
				<Button icon={FileUp} onclick={() => (dialog = { kind: 'csv' })}>{m.migrations_add_csv()}</Button>
			{/snippet}
		</EmptyState>
	{:else if boxes.length}
		<div class="flex flex-wrap items-center gap-1" role="group" aria-label={m.migrations_conn_group()} data-testid="repair-conn">
			<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleCheck size={14} class="text-signal-ink" />{m.migrations_conn_ok({ count: conn.ok })}</span>
			<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleX size={14} class="text-warning-ink" />{m.migrations_conn_failed({ count: conn.failed })}</span>
			<span class="inline-flex min-h-10 items-center gap-1 px-2 text-sm text-ink-2"><CircleDashed size={14} class="text-ink-3" />{m.migrations_conn_untested({ count: conn.untested })}</span>
		</div>

		<Panel>
			<div class="flex items-center gap-3 border-b border-line px-4 py-2">
				<label class="check justify-start gap-2">
					<input type="checkbox" checked={allSelected} indeterminate={selectedIds.length > 0 && !allSelected} onchange={toggleAll} />
					<span class="text-sm text-ink-2">{m.repair_select_all()}</span>
				</label>
			</div>
			<ul class="stagger m-0 flex list-none flex-col divide-y divide-line p-0" data-testid="repair-list">
				{#each boxes as box (box.id)}
					{@const look = repairLook(box)}
					{@const c = connLook(box)}
					{@const Icon = c.icon}
					{@const email = prefs.email(box.email)}
					{@const ready = repairable(box)}
					<li class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2" data-repair-box={box.email}>
						<label class="check">
							<input type="checkbox" checked={repairs.selected.has(box.id)} aria-label={m.migrations_row_select({ email })} onchange={(e) => (e.currentTarget.checked ? repairs.selected.add(box.id) : repairs.selected.delete(box.id))} />
						</label>
						<button type="button" class="flex min-h-10 min-w-0 flex-1 basis-56 items-center gap-2 rounded-btn text-left hover:bg-ghost" onclick={() => (open = open === box.id ? null : box.id)} aria-expanded={open === box.id} title={m.repair_row_expand({ email })} data-testid="repair-open">
							<ChevronRight size={16} aria-hidden="true" class={`shrink-0 text-ink-3 transition-transform duration-200 rtl:rotate-180 ${open === box.id ? 'rotate-90 rtl:rotate-90' : ''}`} />
							<span class="flex min-w-0 flex-col items-start justify-center">
								<span class="flex min-w-0 items-center gap-1.5">
									<span class={`inline-flex shrink-0 ${c.tone}`} title={c.label} data-testid="repair-conn-state" data-conn={repairs.testing.has(box.id) ? 'testing' : connectionOf(box)}>
										<Icon size={14} aria-hidden="true" /><span class="sr-only">{c.label}</span>
									</span>
									<span class="truncate font-medium">{email}</span>
								</span>
								<span class="flex max-w-full gap-2 text-xs text-ink-2" data-testid="repair-line"><span class="truncate">{serverName(box.server_id)}</span>{#if resultLine(box)}<span class="truncate text-ink-3">{resultLine(box)}</span>{/if}</span>
							</span>
						</button>
						<span data-testid="repair-state"><Plate tone={look.tone} icon={look.icon} label={look.label} busy={look.busy} /></span>
						{#if box.active_run}
							<span class="w-32 shrink-0"><Progress value={percent(box)} label={m.repair_row_progress_label({ email })} /></span>
						{:else if ready}
							<Button icon={Wrench} onclick={() => (confirming = { mode: 'repair', ids: [box.id] })} data-testid="repair-next">{m.repair_action_repair()}</Button>
						{:else}
							<Button icon={ScanSearch} onclick={() => run([box.id], 'scan')} data-testid="repair-next">{m.repair_action_scan()}</Button>
						{/if}
						<Menu label={m.migrations_row_more({ email })} icon={EllipsisVertical} items={menu(box)} />
					</li>
					{#if open === box.id && openBox}
						<li class="bg-surface-2/40 px-4 py-3" data-testid="repair-detail">
							<RepairDetail box={openBox} onrepair={() => (confirming = { mode: 'repair', ids: [box.id] })} />
						</li>
					{/if}
				{/each}
			</ul>
		</Panel>

		{#if selectedIds.length}
			<div class="sticky bottom-3 z-30 flex flex-wrap items-center gap-2 rounded-card border border-line-strong bg-surface-2 p-2 shadow-float" data-testid="repair-bulk">
				<span class="px-2 text-sm">{m.migrations_bar_selected({ count: selectedIds.length })}</span>
				<Button icon={ScanSearch} onclick={() => run(selectedIds, 'scan')} data-testid="bulk-scan">{m.repair_action_scan()}</Button>
				<Button icon={Wrench} disabled={!repairIds.length} title={repairIds.length ? undefined : m.repair_reason_scan_first()} onclick={() => (confirming = { mode: 'repair', ids: selectedIds })} data-testid="bulk-repair">{m.repair_action_repair()}</Button>
				<Button icon={Undo2} onclick={() => (confirming = { mode: 'restore', ids: selectedIds })} data-testid="bulk-restore">{m.repair_action_restore()}</Button>
				<Button variant="ghost" icon={FlaskConical} onclick={() => repairs.test(selectedIds)} data-testid="bulk-test">{m.migrations_row_test()}</Button>
				<Button variant="ghost" onclick={() => repairs.selected.clear()}>{m.repair_selection_clear()}</Button>
			</div>
		{/if}
	{/if}
</div>

{#if dialog?.kind === 'add'}
	<RepairBoxDialog oncsv={() => (dialog = { kind: 'csv' })} onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'edit'}
	<RepairBoxDialog box={dialog.box} onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'csv'}
	<RepairCsvDialog onclose={() => (dialog = null)} />
{/if}
{#if confirming?.mode === 'repair'}
	{@const ids = confirming.ids}
	{@const ready = boxes.filter((b) => ids.includes(b.id) && repairable(b))}
	<RepairConfirmDialog boxes={ready} skipped={ids.length - ready.length} onconfirm={() => run(ready.map((b) => b.id), 'repair')} onclose={() => (confirming = null)} />
{:else if confirming}
	{@const c = confirming}
	<Confirm title={m.repair_confirm_restore_title()} text={m.repair_confirm_restore_text({ count: c.ids.length })} confirm={m.repair_action_restore()} onconfirm={() => run(c.ids, 'restore')} onclose={() => (confirming = null)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.repair_delete_title()} text={m.repair_delete_text({ email: prefs.email(target.email) })} confirm={m.repair_action_forget_confirm()} onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
