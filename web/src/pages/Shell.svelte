<script lang="ts">
	import ArrowRightLeft from '@lucide/svelte/icons/arrow-right-left';
	import BookOpen from '@lucide/svelte/icons/book-open';
	import ExternalLink from '@lucide/svelte/icons/external-link';
	import Files from '@lucide/svelte/icons/files';
	import FolderUp from '@lucide/svelte/icons/folder-up';
	import GitBranch from '@lucide/svelte/icons/git-branch';
	import LogOut from '@lucide/svelte/icons/log-out';
	import PanelLeftClose from '@lucide/svelte/icons/panel-left-close';
	import PanelLeftOpen from '@lucide/svelte/icons/panel-left-open';
	import Server from '@lucide/svelte/icons/server';
	import Settings from '@lucide/svelte/icons/settings';
	import Unplug from '@lucide/svelte/icons/unplug';
	import Wrench from '@lucide/svelte/icons/wrench';
	import type { Component } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { post } from '../lib/api/client.ts';
	import { asset } from '../lib/base.ts';
	import { pct } from '../lib/format.ts';
	import { prefs } from '../lib/prefs.svelte.ts';
	import { summarize } from '../lib/logic/accounts.ts';
	import { router } from '../lib/router.svelte.ts';
	import { data } from '../lib/stores/data.svelte.ts';
	import { sources } from '../lib/stores/sources.svelte.ts';
	import IconButton from '../lib/ui/IconButton.svelte';
	import Plate from '../lib/ui/Plate.svelte';
	import { uploads } from '../lib/upload/store.svelte.ts';
	import FilesPage from './files/FilesPage.svelte';
	import MigrationsPage from './migrations/MigrationsPage.svelte';
	import ServersPage from './settings/ServersPage.svelte';
	import DuplicatesPage from './duplicates/DuplicatesPage.svelte';
	import RepairPage from './repair/RepairPage.svelte';
	import SettingsPage from './settings/SettingsPage.svelte';
	import TenantPage from './tenant/TenantPage.svelte';

	/** extra: only the static demo passes one (its badge); the product never does. */
	let { onlogout, extra: Extra }: { onlogout: () => void; extra?: Component<{ part: 'badge' | 'footer' }> } = $props();

	// The matching screen belongs to Migrations.
	const isCurrent = (page: string) =>
		router.route.page === page || (page === 'migrations' && router.route.page === 'tenant') || (page === 'repair' && router.route.page === 'duplicates');

	// Same width as --side-w in tokens.css: a column on the left, else a bar on top. Only one is in the page.
	const wide = new MediaQuery('min-width: 64rem');

	// What each page holds, said next to its link: what needs you, and how many files.
	const summary = $derived(summarize(data.accounts.values(), (id) => data.jobs.get(id)));
	const attention = $derived(summary.attention);
	const NAV = $derived([
		{ page: 'migrations', href: '/', label: m.shell_nav_migrations(), icon: ArrowRightLeft, count: attention, alert: attention > 0, busy: summary.running > 0 },
		{ page: 'files', href: '/files', label: m.shell_nav_files(), icon: Files, count: data.psts.size, alert: false, busy: false },
		{ page: 'servers', href: '/servers', label: m.settings_servers_title(), icon: Server, count: data.servers.length + sources.list.length, alert: false, busy: false },
		// Repair and Duplicates are two tabs of one page.
		{ page: 'repair', href: '/repair', label: m.shell_nav_maintenance(), icon: Wrench, count: 0, alert: false, busy: false }
	]);
	// Settings sits apart, at the bottom of the column.
	const SETTINGS = $derived({ page: 'settings', href: '/settings', label: m.shell_nav_settings(), icon: Settings, count: 0, alert: false, busy: false });
	type NavItem = typeof SETTINGS;
	// Two links out of the app, just above Settings. lucide has no GitHub mark, so a branch stands for the repository.
	const EXTERNAL = $derived([
		{ href: 'https://github.com/selfhostr/mccb/tree/main/docs', label: m.shell_docs(), icon: BookOpen },
		{ href: 'https://github.com/selfhostr/mccb', label: m.shell_github(), icon: GitBranch }
	]);

	// The column can shrink to its icons; the choice is kept in this browser.
	const collapsed = $derived(prefs.value.sidebar === 'collapsed');
	const toggleSide = () => prefs.set('sidebar', collapsed ? 'open' : 'collapsed');
	const SHORTCUT = '[';

	// `[` folds or unfolds the column, unless the person is typing or the key is part of a combination.
	function onkeydown(e: KeyboardEvent) {
		if (e.key !== SHORTCUT || e.repeat || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || !wide.current) return;
		const t = e.target;
		if (t instanceof HTMLElement && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return;
		e.preventDefault();
		toggleSide();
	}

	const sending = $derived(uploads.items.filter((i) => i.status === 'starting' || i.status === 'uploading' || i.status === 'completing'));
	const sendingPercent = $derived.by(() => {
		let done = 0;
		let total = 0;
		for (const item of sending) {
			done += item.sentBytes;
			total += item.size;
		}
		return pct(done, total);
	});

	// Tenants and other saved connections count as servers.
	void sources.load();

	// For a moment after each navigation the page's first rows come in one after the other (app.css, .is-entering).
	let entering = $state(true);
	$effect(() => {
		void router.route.page;
		entering = true;
		const t = setTimeout(() => (entering = false), 480);
		return () => clearTimeout(t);
	});

	async function logout() {
		try {
			await post('/logout');
		} finally {
			onlogout();
		}
	}
</script>

<a class="skip" href="#main">{m.shell_skip()}</a>

<svelte:window {onkeydown} />

{#snippet status(compact = false)}
	{#if compact}
		<!-- In the icon rail: the icon alone, its text in the tooltip and for screen readers. -->
		{#if sending.length}
			<a class="relative inline-flex size-10 items-center justify-center rounded-btn text-ink-2 no-underline transition-colors duration-200 hover:bg-ghost hover:text-ink" href="/files" title={m.shell_uploading({ percent: sendingPercent })} data-testid="upload-indicator">
				<span class="busy inline-flex"><FolderUp size={18} /></span>
				<span class="sr-only">{m.shell_uploading({ percent: sendingPercent })}</span>
			</a>
		{/if}
		{#if !data.live}
			<span class="inline-flex size-10 items-center justify-center rounded-btn text-warning-ink" title={m.shell_reconnecting()} data-testid="live">
				<Unplug size={18} />
				<span class="sr-only">{m.shell_reconnecting()}</span>
			</span>
		{/if}
	{:else}
	{#if sending.length}
		<a class="inline-flex min-h-10 items-center gap-2 rounded-btn px-2 text-ink-2 no-underline transition-colors duration-200 hover:text-ink" href="/files" data-testid="upload-indicator">
			<span class="busy inline-flex"><FolderUp size={14} /></span>
			<span class="font-mono text-xs">{m.shell_uploading({ percent: sendingPercent })}</span>
		</a>
	{/if}
	<!-- Said only when it matters: a page that updates by itself needs no "Live" label. -->
	{#if !data.live}
		<span class="min-w-0" data-testid="live"><Plate tone="advisory" icon={Unplug} label={m.shell_reconnecting()} /></span>
	{/if}
	{/if}
{/snippet}

{#snippet external(item: (typeof EXTERNAL)[number])}
	{@const Icon = item.icon}
	<a
		class={`flex min-h-10 items-center rounded-btn text-ink-2 no-underline transition-colors duration-200 hover:bg-ghost hover:text-ink ${collapsed ? 'w-10 justify-center' : 'gap-3 px-3'}`}
		href={item.href}
		target="_blank"
		rel="noopener noreferrer"
		title={collapsed ? item.label : undefined}
	>
		<Icon size={18} />
		<span class={collapsed ? 'sr-only' : 'min-w-0 flex-1 truncate'}>{item.label}</span>
		{#if !collapsed}<ExternalLink size={12} class="shrink-0 text-ink-3" aria-hidden="true" />{/if}
		<span class="sr-only">{m.shell_new_tab()}</span>
	</a>
{/snippet}

{#snippet link(item: NavItem)}
	{@const Icon = item.icon}
	{@const current = isCurrent(item.page)}
	<a
		class={`relative flex min-h-10 items-center rounded-btn no-underline transition-colors duration-200 ${collapsed ? 'w-10 justify-center' : 'gap-3 px-3'} ${current ? 'bg-surface-2 font-medium text-ink' : 'text-ink-2 hover:bg-ghost hover:text-ink'}`}
		href={item.href}
		title={collapsed ? item.label : undefined}
		aria-current={current ? 'page' : undefined}
	>
		<!-- The current page's mark grows in as it is reached; a slow dot says that work is running there. -->
		{#if current}<span class="nav-mark absolute inset-y-2.5 left-0 w-0.5 rounded-full bg-ink" aria-hidden="true"></span>{/if}
		<Icon size={18} />
		<span class={collapsed ? 'sr-only' : 'min-w-0 flex-1 truncate'}>{item.label}</span>
		{#if item.busy && !collapsed && !item.alert}<span class="live-dot" aria-hidden="true"></span>{/if}
		{#if item.count && !collapsed}
			<span class={`rounded-plate px-1.5 font-mono text-xs ${item.alert ? 'bg-warning-soft text-warning-ink' : 'text-ink-3'}`}>{item.count}</span>
		{:else if item.alert}
			<span class="absolute top-2 right-2 size-1.5 rounded-full bg-warning" aria-hidden="true"></span>
		{:else if item.busy && collapsed}
			<span class="live-dot absolute top-2 right-2" aria-hidden="true"></span>
		{/if}
	</a>
{/snippet}

{#if wide.current}
	<aside class="fixed top-0 bottom-0 left-0 z-40 flex w-[var(--side-w)] flex-col border-r border-line bg-canvas-deep" data-testid="sidebar" data-collapsed={collapsed}>
		<!-- The fold button sits beside the logo, where it is seen first; in the rail it goes under it. -->
		<div class={`flex items-center ${collapsed ? 'flex-col gap-2 pt-5 pb-3' : 'h-20 gap-2 pt-2 pr-3 pl-5'}`}>
			<a class={`flex items-center gap-2 no-underline ${collapsed ? 'size-10 justify-center' : 'min-h-10 min-w-0 flex-1'}`} href="/" aria-label={m.shell_home()}>
				<img src={asset('favicon.svg')} alt="" width="24" height="24" />
				{#if !collapsed}<span class="text-base font-semibold">mccb</span>{/if}
			</a>
			<IconButton
				variant="outline"
				icon={collapsed ? PanelLeftOpen : PanelLeftClose}
				label={`${collapsed ? m.shell_expand() : m.shell_collapse()} (${SHORTCUT})`}
				aria-expanded={!collapsed}
				aria-keyshortcuts={SHORTCUT}
				onclick={toggleSide}
				data-testid="sidebar-toggle"
			/>
		</div>
		{#if Extra && !collapsed}<div class="px-4 pb-2"><Extra part="badge" /></div>{/if}
		<nav class={`flex flex-1 flex-col gap-0.5 ${collapsed ? 'items-center' : 'px-3'}`} aria-label={m.shell_nav_label()}>
			{#each NAV as item (item.page)}{@render link(item)}{/each}
			<span class="flex-1"></span>
			{#if sending.length || !data.live}
				<div class={collapsed ? 'flex flex-col items-center gap-0.5 pb-1' : 'flex flex-wrap items-center gap-2 px-1 pb-1'}>{@render status(collapsed)}</div>
			{/if}
			{#each EXTERNAL as item (item.href)}{@render external(item)}{/each}
			{@render link(SETTINGS)}
		</nav>
		<div class={`mt-2 flex items-center border-t border-line py-2 ${collapsed ? 'flex-col gap-1' : 'gap-1 px-3'}`}>
			<!-- Rail: the version in small type, cut to the column; the tooltip has all of it. -->
			{#if data.version}<span class={collapsed ? 'w-full truncate px-1 text-center font-mono text-[10px] text-ink-3' : 'min-w-0 flex-1 truncate font-mono text-xs text-ink-3'} title={data.version}>{data.version}</span>{:else if !collapsed}<span class="flex-1"></span>{/if}
			<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
		</div>
	</aside>
{:else}
	<header class="fixed inset-x-0 top-0 z-40 h-[var(--bar-h)] border-b border-line bg-canvas">
		<div class="flex h-full flex-wrap content-center items-center gap-x-3 px-[var(--gutter)]">
			<a class="flex min-h-10 items-center gap-2 no-underline" href="/" aria-label={m.shell_home()}>
				<img src={asset('favicon.svg')} alt="" width="24" height="24" />
				<span class="text-base font-semibold">mccb</span>
			</a>
			<nav class="order-last -mx-1 flex w-full gap-0.5 overflow-x-auto px-1 [scrollbar-width:none] sm:order-none sm:ml-2 sm:mx-0 sm:w-auto sm:overflow-visible sm:px-0 [&::-webkit-scrollbar]:hidden" aria-label={m.shell_nav_label()}>
				{#each [...NAV, SETTINGS] as item (item.page)}
					<a
						class={`inline-flex min-h-10 shrink-0 items-center whitespace-nowrap rounded-btn px-3 no-underline transition-colors duration-200 ${isCurrent(item.page) ? 'bg-surface-2 font-medium text-ink' : 'text-ink-2 hover:text-ink'}`}
						href={item.href}
						aria-current={isCurrent(item.page) ? 'page' : undefined}
					>
						{item.label}
						{#if item.busy && !item.alert}<span class="live-dot ml-2" aria-hidden="true"></span>{/if}
					</a>
				{/each}
			</nav>
			<span class="flex min-w-0 flex-1 justify-center">{#if Extra}<Extra part="badge" />{/if}</span>
			{@render status()}
			<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
		</div>
	</header>
{/if}

<main id="main" class={`px-[var(--gutter)] pt-[var(--bar-h)] pb-28 lg:ml-[var(--side-w)] lg:px-8 ${entering ? 'is-entering' : ''}`}>
	<!-- A new page fades in with a short rise (app.css .page-enter); a change inside a page does not. -->
	{#key router.route.page}<div class="page-enter">
	{#if router.route.page === 'migrations'}
		<MigrationsPage />
	{:else if router.route.page === 'files'}
		<FilesPage />
	{:else if router.route.page === 'servers'}
		<ServersPage />
	{:else if router.route.page === 'repair'}
		<RepairPage />
	{:else if router.route.page === 'duplicates'}
		<DuplicatesPage />
	{:else if router.route.page === 'tenant'}
		<TenantPage />
	{:else}
		<SettingsPage />
	{/if}
	</div>{/key}
</main>

{#if Extra}<Extra part="footer" />{/if}
