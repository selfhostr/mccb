<script lang="ts">
	/**
	 * One row. It receives an id and reads only its own map entries, and every value used by
	 * the markup is a $derived primitive: a live event for another account runs nothing here,
	 * and a progress event for this one only reaches ProgressCell.
	 */
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CircleX from '@lucide/svelte/icons/circle-x';
	import ArrowUp from '@lucide/svelte/icons/arrow-up';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import KeyRound from '@lucide/svelte/icons/key-round';
	import Route from '@lucide/svelte/icons/route';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account } from '../../lib/api/types.ts';
	import { connectionOf } from '../../lib/logic/accounts.ts';
	import { endpointSummary, hasCustomSource } from '../../lib/logic/connector.ts';
	import { rowHref } from '../../lib/logic/routes.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { accountLook, jobLookOf, testLook } from '../../lib/status.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ProgressCell from './ProgressCell.svelte';

	interface Props {
		id: number;
		onedit: (account: Account) => void;
		ondelete: (account: Account) => void;
		onroute: (account: Account) => void;
	}

	let { id, onedit, ondelete, onroute }: Props = $props();

	// A deleted row may still run once before it goes: it keeps showing what it last knew.
	let last: Account | undefined;
	const account = $derived.by(() => (last = data.accounts.get(id) ?? last) as Account);
	const email = $derived(prefs.email(account.email));
	const selected = $derived(data.selected.has(id));
	const phase = $derived(data.testing.get(id));

	// Primitives: a progress event for this job changes none of these.
	const jobId = $derived(data.jobs.get(id)?.id);
	const jobStatus = $derived(data.jobs.get(id)?.status);
	const pauseReason = $derived(data.jobs.get(id)?.pause_reason);
	const custom = $derived(hasCustomSource(account));
	const look = $derived.by(() => {
		if (phase) return testLook(phase);
		if (jobStatus) return jobLookOf({ status: jobStatus, pause_reason: pauseReason });
		return accountLook(account.status, custom || account.pst_id != null);
	});

	// The connection at a glance, next to the address: verified, refused or never tested.
	const conn = $derived.by(() => {
		if (phase) return { icon: FlaskConical, tone: 'text-info-ink', label: m.migrations_test_running() };
		const c = connectionOf(account);
		if (c === 'ok') return { icon: CircleCheck, tone: 'text-signal-ink', label: m.migrations_test_status_ok() };
		if (c === 'untested') return { icon: CircleDashed, tone: 'text-ink-3', label: m.migrations_test_never() };
		return { icon: CircleX, tone: 'text-warning-ink', label: account.status === 'auth_failed' ? m.migrations_row_login_refused() : m.migrations_test_status_failed() };
	});
	const ConnIcon = $derived(conn.icon);

	// The route in words: where the mail comes from and where it goes.
	const fileName = $derived(account.pst_id != null ? prefs.file(data.psts.get(account.pst_id)?.filename) : '');
	const sourceLabel = $derived(endpointSummary(connectors.list, account.source, 'source')?.label ?? '');
	const targetLabel = $derived(endpointSummary(connectors.list, account.target, 'target')?.label ?? '');
	const serverName = $derived(data.servers.find((s) => s.id === account.server_id)?.name ?? '');
	const route = $derived(m.migrations_row_route_line({ source: sourceLabel || fileName || m.migrations_row_pst_none(), target: targetLabel || serverName }));
	const canRoute = $derived(connectors.list.some((c) => c.source && c.kind !== 'pst') || connectors.list.some((c) => c.target));
	const href = $derived(rowHref(router.search, id, jobId));

	function toggleSelected(e: Event & { currentTarget: HTMLInputElement }) {
		if (e.currentTarget.checked) data.selected.add(id);
		else data.selected.delete(id);
	}

	async function move(step: -1 | 1) {
		if (await data.moveQueued(id, step)) toast(step < 0 ? m.migrations_row_moved_up() : m.migrations_row_moved_down(), 'signal');
	}

	const menu = $derived([
		{ label: phase ? m.migrations_test_running() : m.migrations_row_test(), icon: FlaskConical, disabled: !!phase, onselect: () => data.test([id]) },
		...(jobStatus === 'queued'
			? [
					{ label: m.migrations_row_queue_up(), icon: ArrowUp, onselect: () => move(-1) },
					{ label: m.migrations_row_queue_down(), icon: ArrowDown, onselect: () => move(1) }
				]
			: []),
		{ label: m.migrations_row_edit(), icon: KeyRound, onselect: () => onedit(account) },
		...(canRoute || sourceLabel || targetLabel ? [{ label: m.migrations_row_route(), icon: Route, onselect: () => onroute(account) }] : []),
		{ label: m.migrations_row_delete(), icon: Trash2, danger: true, onselect: () => ondelete(account) }
	]);
</script>

<label class="check acct-sel">
	<input type="checkbox" checked={selected} aria-label={m.migrations_row_select({ email })} onchange={toggleSelected} />
</label>
<div class="acct-who">
	<a class="acct-link" {href} data-account-link={id} data-job-link={jobId}>
		<span class="flex min-w-0 items-center gap-1.5">
			<span class={`inline-flex shrink-0 ${conn.tone}`} title={conn.label} data-testid="account-conn" data-conn={phase ? 'testing' : connectionOf(account)}>
				<ConnIcon size={14} aria-hidden="true" /><span class="sr-only">{conn.label}</span>
			</span>
			<span class="truncate font-semibold" title={email} data-testid="account-email">{email}</span>
		</span>
		<span class="truncate text-xs text-ink-2">{route}</span>
	</a>
</div>
<div class="acct-status" data-testid={phase ? 'test-state' : 'row-status'}>
	<Plate tone={look.tone} icon={look.icon} label={look.label} />
</div>
<div class="acct-bar">
	<ProgressCell accountId={id} />
</div>
<div class="acct-menu">
	<Menu label={m.migrations_row_more({ email })} icon={EllipsisVertical} items={menu} />
</div>
