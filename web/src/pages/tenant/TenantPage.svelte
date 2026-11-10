<script lang="ts">
	/**
	 * Matching screen: every mailbox of a Microsoft 365 tenant beside the mailboxes of one server, paired by
	 * address on the server side. Tick, Start: the ticked pairs are linked to the tenant and migrate in one named
	 * batch. After the cutover, the same screen runs the final resync (delta) on the linked rows.
	 */
	import ArrowLeft from '@lucide/svelte/icons/arrow-left';
	import ArrowRight from '@lucide/svelte/icons/arrow-right';
	import Building from '@lucide/svelte/icons/building';
	import Info from '@lucide/svelte/icons/info';
	import ListChecks from '@lucide/svelte/icons/list-checks';
	import Mail from '@lucide/svelte/icons/mail';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Search from '@lucide/svelte/icons/search';
	import SearchX from '@lucide/svelte/icons/search-x';
	import Server from '@lucide/svelte/icons/server';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { onMount, untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, put } from '../../lib/api/client.ts';
	import type { Account, LinkResponse, MatchResponse, MatchRow as MatchRowT, MatchState } from '../../lib/api/types.ts';
	import {
		STATE_ORDER,
		baseRows,
		bestServer,
		canFinalResync,
		countStates,
		coverageSummary,
		hiddenTicked,
		isResource,
		landingKinds,
		linksOf,
		parseStateFilter,
		pretick,
		rowKey,
		sortRows,
		targetRows,
		tasksAllowed,
		tickHeader,
		tickReady,
		toggleVisible
	} from '../../lib/logic/tenantmatch.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import MatchRow from './MatchRow.svelte';
	import PickDialog from './PickDialog.svelte';
	import StartDialog from './StartDialog.svelte';
	import TenantBar from './TenantBar.svelte';
	import { COVERAGE_STATES, chipLabel, coverageLook, kindName, matchLook } from './looks.ts';

	onMount(() => {
		void sources.load();
	});

	const tenantId = $derived(router.route.tenantId ?? 0);
	const serverId = $derived(Number(router.params.get('server')) || null);
	const stateFilter = $derived(parseStateFilter(router.params.get('state')));
	const q = $derived(router.params.get('q') ?? '');
	const rooms = $derived(router.params.get('rooms') === '1');

	let result = $state.raw<MatchResponse | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);
	let refreshing = $state(false);
	let ticked = $state.raw<Set<string>>(new Set());
	let dialog = $state<'full' | 'delta' | null>(null);
	let testIds = $state.raw<number[]>([]);
	// The row whose destination is being chosen by hand, and the one going back to automatic.
	let picking = $state.raw<MatchRowT | null>(null);
	let resetting = $state<number | null>(null);
	// Why the last Start left some mailboxes out (account id -> message), shown on their rows.
	let linkErrors = $state.raw<Map<number, string>>(new Map());
	const BATCH_MAX = 120; // as the API accepts
	// Not reactive: what the ticks were made on, so that a refresh keeps the choices and a new server starts fresh.
	let known = new Set<string>();
	let scope = '';
	let seq = 0;

	async function load(refresh = false) {
		const id = tenantId;
		const sid = serverId;
		if (!id || !sid) return;
		const mine = ++seq;
		const next = `${id}:${sid}`;
		if (next !== scope) result = null;
		if (refresh) refreshing = true;
		else if (!result) loading = true;
		try {
			const res = await sources.match(id, sid, refresh);
			if (mine !== seq) return;
			ticked = pretick(res.rows, next === scope ? { ticked, known } : undefined);
			known = new Set(res.rows.map(rowKey));
			scope = next;
			result = res;
			error = null;
		} catch (err) {
			if (mine === seq) error = errorText(err);
		} finally {
			if (mine === seq) {
				loading = false;
				refreshing = false;
			}
		}
	}

	$effect(() => {
		if (tenantId && serverId) untrack(() => load(false));
	});

	// No server in the address: take the one that holds most of the tenant's addresses, and say so in the URL.
	let choosingServer = false;
	$effect(() => {
		if (serverId || !tenantId || data.loading || !data.servers.length) return;
		untrack(pickServer);
	});

	async function pickServer() {
		if (choosingServer) return;
		choosingServer = true;
		const id = tenantId;
		try {
			let best = data.servers[0].id;
			if (data.servers.length > 1) {
				try {
					const list = await sources.mailboxes(id);
					best = bestServer(data.servers, data.accounts.values(), list.mailboxes) ?? best;
				} catch {
					// The match will say what is wrong with the tenant.
				}
			}
			if (router.route.page === 'tenant' && router.route.tenantId === id && !Number(router.params.get('server'))) router.setParams({ server: String(best) });
		} finally {
			choosingServer = false;
		}
	}

	// Tests asked from here: once they are all done, the coverage (which reads the test results) is fetched again.
	const testing = $derived(testIds.some((id) => data.testing.has(id)));
	$effect(() => {
		if (testIds.length && !testing)
			untrack(() => {
				testIds = [];
				void load(false);
			});
	});

	const rows = $derived(result ? sortRows(result.rows) : []);
	const base = $derived(baseRows(rows, { q, rooms }));
	const counts = $derived(countStates(base));
	const visible = $derived(stateFilter ? base.filter((r) => r.state === stateFilter) : base);
	const targets = $derived(targetRows(visible, ticked));
	const hidden = $derived(hiddenTicked(rows, visible, ticked));
	const header = $derived(tickHeader(visible, ticked));
	const resources = $derived(rows.filter(isResource).length);
	const readyVisible = $derived(visible.some((r) => r.state === 'ready'));

	const connection = $derived(sources.byId(tenantId));
	const tenantName = $derived(result?.connection.name ?? connection?.name ?? '');
	const server = $derived(data.servers.find((s) => s.id === serverId) ?? null);
	const serverName = $derived(result?.server.name ?? server?.name ?? '');
	const noDav = $derived(!!result && !result.server.dav_enabled);
	const davFailed = $derived(!!result && result.server.dav_enabled && (result.server.caldav_ok === false || result.server.carddav_ok === false));
	const tasksMissing = $derived(rows.some((r) => r.coverage?.tasks === 'no_permission'));
	const missing = $derived(sources.loaded && !sources.error && !connection && !result);

	const kinds = $derived(landingKinds(targets).map(kindName));
	const summary = $derived(coverageSummary(targets));

	const TONE_TEXT = { signal: 'text-signal-ink', info: 'text-info-ink', advisory: 'text-advisory-ink', warning: 'text-warning-ink', ghost: 'text-ink-2' };
	const CHIPS = $derived([{ value: '' as const, count: counts.all }, ...STATE_ORDER.map((s) => ({ value: s, count: counts[s] }))]);

	function setState(value: MatchState | '') {
		router.setParams({ state: value && value !== stateFilter ? value : null });
	}

	function toggle(key: string, on: boolean) {
		const next = new Set(ticked);
		if (on) next.add(key);
		else next.delete(key);
		ticked = next;
	}

	function showAll() {
		router.setParams({ state: null, q: null, rooms: resources ? '1' : null });
	}

	function test() {
		const ids = targets.map((r) => r.account!.id);
		data.test(ids);
		testIds = ids;
	}

	async function picked(row: MatchRowT, email: string) {
		await load(false);
		toast(m.tenant_pick_done({ mailbox: prefs.email(row.mailbox?.mail ?? ''), email: prefs.email(email) }), 'signal');
	}

	/** Back to automatic: the box lets the mailbox go (as before the choice), the match by address comes back. */
	async function reset(row: MatchRowT) {
		if (!row.account) return;
		resetting = row.account.id;
		try {
			data.upsertAccount(await put<Account>(`/accounts/${row.account.id}/source`, {}));
			await load(false);
			toast(m.tenant_pick_reset_done({ mailbox: prefs.email(row.mailbox?.mail ?? '') }), 'signal');
		} catch (err) {
			toast(errorText(err), 'warning', 9000);
		} finally {
			resetting = null;
		}
	}

	function batchName(mode: 'full' | 'delta'): string {
		const date = new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: 'medium' }).format(new Date());
		return (mode === 'delta' ? m.tenant_batch_name_resync({ tenant: tenantName, server: serverName, date }) : m.tenant_batch_name({ tenant: tenantName, server: serverName, date })).slice(0, BATCH_MAX);
	}

	/** The per-mailbox refusals of a link: in the answer, or in the 409 when none was linked. */
	function refusals(err: unknown): LinkResponse['errors'] | null {
		if (!(err instanceof ApiError) || err.status !== 409 || !err.body || typeof err.body !== 'object') return null;
		const detail = (err.body as { detail?: unknown }).detail;
		const errors = detail && typeof detail === 'object' ? (detail as { errors?: unknown }).errors : null;
		return Array.isArray(errors) && errors.length ? (errors as LinkResponse['errors']) : null;
	}

	function describe(errors: LinkResponse['errors'], chosen: MatchRowT[]): string {
		const first = errors[0];
		const email = chosen.find((r) => r.account?.id === first.account_id)?.account?.email ?? '';
		return m.tenant_link_errors({ count: errors.length, email: prefs.email(email), msg: first.msg });
	}

	async function start(mode: 'full' | 'delta', name: string) {
		if (!serverId) return;
		const chosen = targets;
		linkErrors = new Map();
		let res: LinkResponse;
		try {
			res = await sources.link(tenantId, {
				server_id: serverId,
				links: linksOf(chosen),
				include_tasks: tasksAllowed(chosen),
				start: true,
				mode,
				batch_name: name.slice(0, BATCH_MAX)
			});
		} catch (err) {
			// Nothing linked: the dialog stays open and says why, each row says its own reason.
			const errors = refusals(err);
			if (!errors) throw err;
			linkErrors = new Map(errors.map((e) => [e.account_id, e.msg]));
			void load(false);
			throw new Error(describe(errors, chosen));
		}
		for (const job of res.jobs ?? []) data.upsertJob(job);
		if (res.errors.length) {
			linkErrors = new Map(res.errors.map((e) => [e.account_id, e.msg]));
			toast(describe(res.errors, chosen), 'warning', 9000);
		}
		toast(mode === 'delta' ? m.tenant_resync_started({ count: res.linked.length, batch: name }) : m.tenant_started({ count: res.linked.length, batch: name }), 'signal');
		if (res.batch_id != null) {
			await data.loadBatches();
			router.go(`/?batch=${res.batch_id}`);
		} else void load(false);
	}
</script>

{#snippet info(text: string, testid: string)}
	<div class="flex items-start gap-2 rounded-btn border border-line-strong bg-surface-2 p-3 text-sm text-ink-2" data-testid={testid}>
		<span class="mt-0.5 text-info-ink"><Info size={14} /></span>
		<p class="m-0 min-w-0">{text}</p>
	</div>
{/snippet}

<div class="flex flex-col gap-4 pb-24 sm:pb-8" data-testid="tenant-page">
	<header class="flex flex-col gap-3 pt-5">
		<a class="inline-flex min-h-10 w-fit items-center gap-1.5 text-sm text-ink-2 no-underline hover:text-ink" href="/"><ArrowLeft size={14} />{m.shell_nav_migrations()}</a>
		<div class="flex flex-wrap items-end justify-between gap-3">
			<div class="flex min-w-0 flex-1 basis-80 flex-col gap-2">
				<h1 class="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-xl">
					<span class="inline-flex min-w-0 items-center gap-2"><Building size={20} class="shrink-0 text-ink-3" /><span class="truncate">{m.tenant_title({ tenant: tenantName || '...' })}</span></span>
					<ArrowRight size={20} class="shrink-0 text-ink-3" aria-hidden="true" />
					<span class="sr-only">{m.tenant_title_to()}</span>
					<span class="inline-flex min-w-0 items-center gap-2"><Server size={20} class="shrink-0 text-ink-3" /><span class="truncate">{serverName || '...'}</span></span>
				</h1>
				<p class="m-0 text-sm text-ink-2" data-testid="page-sentence">
					{#if result}
						{m.tenant_sentence({ count: rows.length })}
						<span class="text-ink-3">· {m.tenant_fetched()} <RelTime value={result.fetched_at} /></span>
					{:else}
						{m.tenant_sentence_loading()}
					{/if}
				</p>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				{#if data.servers.length > 1}
					<label class="flex items-center gap-2 text-sm text-ink-2">
						<span>{m.tenant_server_pick()}</span>
						<select class="input w-auto max-w-56" value={serverId == null ? '' : String(serverId)} onchange={(e) => router.setParams({ server: e.currentTarget.value || null })} data-testid="tenant-server">
							{#if serverId == null}<option value="">{m.common_loading()}</option>{/if}
							{#each data.servers as s (s.id)}<option value={String(s.id)}>{s.name}{s.dav_enabled ? '' : ` (${m.tenant_server_mail_only()})`}</option>{/each}
						</select>
					</label>
				{/if}
				<Button icon={RefreshCw} loading={refreshing} disabled={!result} onclick={() => load(true)} data-testid="tenant-refresh">{m.tenant_refresh()}</Button>
			</div>
		</div>
	</header>

	{#if missing}
		<EmptyState icon={Building} title={m.tenant_missing_title()} description={m.tenant_missing_text()}>
			{#snippet action()}<Button variant="primary" href="/servers">{m.tenant_go_servers()}</Button>{/snippet}
		</EmptyState>
	{:else if !data.loading && !data.servers.length}
		<EmptyState icon={Server} title={m.migrations_empty_no_server_title()} description={m.tenant_no_server_text()}>
			{#snippet action()}<Button variant="primary" href="/servers">{m.migrations_empty_no_server_action()}</Button>{/snippet}
		</EmptyState>
	{:else if error && !result}
		<ErrorNotice title={m.tenant_load_failed()} message={error} onretry={() => load(false)} />
	{:else if !result}
		<div class="flex flex-col gap-2" data-testid="skeleton">
			{#each [0, 1, 2, 3, 4, 5] as i (i)}<Skeleton class="h-[var(--row-h)]" />{/each}
		</div>
	{:else}
		{#if error}<ErrorNotice title={m.tenant_load_failed()} message={error} onretry={() => load(false)} />{/if}

		{#if noDav}
			<div class="flex flex-wrap items-start gap-3 rounded-card border-2 border-warning/60 bg-warning-soft p-4 text-warning-ink" role="alert" data-testid="tenant-no-dav">
				<span class="mt-0.5"><TriangleAlert size={20} /></span>
				<div class="min-w-0 flex-1 basis-64">
					<p class="m-0 text-base font-semibold">{m.tenant_banner_no_dav_title({ server: serverName })}</p>
					<p class="m-0 mt-1 text-sm">{m.tenant_banner_no_dav_text()}</p>
				</div>
				<Button href="/servers" data-testid="tenant-edit-server">{m.tenant_banner_edit_server()}</Button>
			</div>
		{:else if davFailed}
			<Notice>
				<span data-testid="tenant-dav-failed">{m.tenant_banner_dav_failed({ server: serverName })} <a href="/servers">{m.tenant_banner_edit_server()}</a></span>
			</Notice>
		{/if}
		{#if tasksMissing}{@render info(m.tenant_banner_tasks(), 'tenant-tasks-info')}{/if}
		{#if !result.purpose_checked}{@render info(m.tenant_banner_purpose(), 'tenant-purpose-info')}{/if}

		{#if !rows.length}
			<EmptyState icon={Building} title={m.tenant_empty_title()} description={m.tenant_empty_text()} />
		{:else}
			<div class="flex flex-col gap-2">
				<div class="flex flex-wrap items-center gap-1.5" role="group" aria-label={m.tenant_chips_label()} data-testid="tenant-chips">
					{#each CHIPS as chip (chip.value)}
						{@const look = chip.value ? matchLook(chip.value, serverName) : null}
						{@const pressed = stateFilter === chip.value}
						<button
							type="button"
							class={`inline-flex min-h-10 items-center gap-1.5 rounded-btn border px-3 text-sm font-medium transition-colors duration-200 disabled:opacity-50 ${pressed ? 'border-ink-3 bg-surface-2 text-ink' : 'border-line-strong text-ink-2 hover:text-ink'}`}
							aria-pressed={pressed}
							disabled={!pressed && !!chip.value && !chip.count}
							onclick={() => setState(chip.value)}
							data-testid={`tenant-chip-${chip.value || 'all'}`}
						>
							{#if look}{@const Icon = look.icon}<Icon size={14} class={chip.count ? TONE_TEXT[look.tone] : ''} />{/if}
							{chipLabel(chip.value || 'all')}
							<span class="font-mono text-ink-2">{chip.count}</span>
						</button>
					{/each}
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<label class="relative min-w-40 flex-1 basis-56 sm:max-w-[320px]">
						<span class="sr-only">{m.tenant_search()}</span>
						<span class="pointer-events-none absolute top-3 left-3 text-ink-3"><Search size={14} /></span>
						<input class="input pl-8" type="search" placeholder={m.tenant_search()} value={q} oninput={(e) => router.setParams({ q: e.currentTarget.value || null })} data-testid="tenant-q" />
					</label>
					<Toggle checked={rooms} label={m.tenant_rooms({ count: resources })} onchange={(on) => router.setParams({ rooms: on ? '1' : null })} />
					{#if readyVisible}
						<Button variant="ghost" icon={ListChecks} class="ml-auto" onclick={() => (ticked = tickReady(visible, ticked))} data-testid="tenant-tick-ready">{m.tenant_tick_ready()}</Button>
					{/if}
				</div>
			</div>

			<Panel>
				<div class="grid grid-cols-[2.5rem_minmax(0,1fr)] items-center gap-x-2 border-b border-line px-3 py-1.5 text-xs font-semibold text-ink-2 md:grid-cols-[2.5rem_minmax(0,1.15fr)_11rem_minmax(0,1fr)_7.5rem] md:gap-x-3">
					<label class="check">
						<input
							type="checkbox"
							checked={header === 'all'}
							indeterminate={header === 'some'}
							disabled={!visible.some((r) => r.state === 'ready' || r.state === 'linked')}
							onchange={() => (ticked = toggleVisible(visible, ticked))}
							aria-label={m.tenant_tick_visible()}
							data-testid="tenant-tick-all"
						/>
					</label>
					<span class="truncate"><span class="md:hidden">{m.tenant_tick_visible()}</span><span class="hidden md:inline">{m.tenant_col_source()}</span></span>
					<span class="hidden md:block">{m.tenant_col_state()}</span>
					<span class="hidden truncate md:block">{m.tenant_col_target({ server: serverName })}</span>
					<span class="hidden md:block">{m.tenant_cov_label()}</span>
				</div>
				{#if visible.length}
					<ul class="m-0 flex list-none flex-col divide-y divide-line p-0" aria-label={m.tenant_list_label()} data-testid="tenant-list">
						{#each visible as row (rowKey(row))}
							{@const key = rowKey(row)}
							<MatchRow
								{row}
								server={serverName}
								ticked={ticked.has(key)}
								problem={row.account ? linkErrors.get(row.account.id) : undefined}
								ontoggle={(on) => toggle(key, on)}
								onpick={row.mailbox ? () => (picking = row) : undefined}
								onreset={() => reset(row)}
								resetting={resetting != null && resetting === row.account?.id}
							/>
						{/each}
					</ul>
				{:else}
					<EmptyState icon={SearchX} title={m.tenant_no_match_title()}>
						{#snippet action()}<Button onclick={showAll}>{m.tenant_bar_show_all()}</Button>{/snippet}
					</EmptyState>
				{/if}
			</Panel>

			<div class="flex flex-col gap-1 text-xs text-ink-3">
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="tenant-legend">
					<span>{m.tenant_legend()}</span>
					{#each COVERAGE_STATES as state (state)}
						{@const look = coverageLook(state)}
						<span class="inline-flex items-center gap-1">
							<span class={`relative inline-flex size-5 items-center justify-center ${look.tone}`} aria-hidden="true">
								<Mail size={14} />
								{#if look.mark}{@const Mark = look.mark}<span class="absolute -right-0.5 -bottom-0.5 inline-flex rounded-full bg-canvas"><Mark size={9} strokeWidth={3} /></span>{/if}
							</span>
							{look.label}
						</span>
					{/each}
				</div>
				{#if result.hidden.guests || result.hidden.no_mailbox}
					<p class="m-0" data-testid="tenant-hidden">{m.tenant_hidden_note({ guests: result.hidden.guests, none: result.hidden.no_mailbox })}</p>
				{/if}
			</div>
		{/if}
	{/if}
</div>

{#if result && rows.length}
	<TenantBar
		count={targets.length}
		{kinds}
		server={serverName}
		{hidden}
		{testing}
		canResync={canFinalResync(targets)}
		ontest={test}
		onresync={() => (dialog = 'delta')}
		onstart={() => (dialog = 'full')}
		onshowHidden={showAll}
	/>
{/if}

{#if picking && result && serverId}
	{@const row = picking}
	<PickDialog {row} rows={result.rows} {tenantId} {serverId} server={serverName} ondone={(_id, email) => picked(row, email)} onclose={() => (picking = null)} />
{/if}

{#if dialog && result}
	{@const mode = dialog}
	<StartDialog
		{mode}
		count={targets.length}
		tenant={tenantName}
		server={serverName}
		{noDav}
		{summary}
		defaultName={batchName(mode)}
		onconfirm={(name) => start(mode, name)}
		onclose={() => (dialog = null)}
	/>
{/if}
