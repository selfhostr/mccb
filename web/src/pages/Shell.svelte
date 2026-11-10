<script lang="ts">
	import ArrowRightLeft from '@lucide/svelte/icons/arrow-right-left';
	import Files from '@lucide/svelte/icons/files';
	import FolderUp from '@lucide/svelte/icons/folder-up';
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
	const attention = $derived(summarize(data.accounts.values(), (id) => data.jobs.get(id)).attention);
	const NAV = $derived([
		{ page: 'migrations', href: '/', label: m.shell_nav_migrations(), icon: ArrowRightLeft, count: attention, alert: attention > 0 },
		{ page: 'files', href: '/files', label: m.shell_nav_files(), icon: Files, count: data.psts.size, alert: false },
		{ page: 'servers', href: '/servers', label: m.settings_servers_title(), icon: Server, count: data.servers.length + sources.list.length, alert: false },
		// Repair and Duplicates are two tabs of one page.
		{ page: 'repair', href: '/repair', label: m.shell_nav_maintenance(), icon: Wrench, count: 0, alert: false }
	]);
	// Settings sits apart, at the bottom of the column.
	const SETTINGS = $derived({ page: 'settings', href: '/settings', label: m.shell_nav_settings(), icon: Settings, count: 0, alert: false });
	type NavItem = typeof SETTINGS;

	// The column can shrink to its icons; the choice is kept in this browser.
	const collapsed = $derived(prefs.value.sidebar === 'collapsed');

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

	async function logout() {
		try {
			await post('/logout');
		} finally {
			onlogout();
		}
	}
</script>

<a class="skip" href="#main">{m.shell_skip()}</a>

{#snippet status()}
	{#if sending.length}
		<a class="inline-flex min-h-10 items-center gap-2 rounded-btn px-2 text-ink-2 no-underline transition-colors duration-200 hover:text-ink" href="/files" data-testid="upload-indicator">
			<FolderUp size={14} />
			<span class="font-mono text-xs">{m.shell_uploading({ percent: sendingPercent })}</span>
		</a>
	{/if}
	<!-- Said only when it matters: a page that updates by itself needs no "Live" label. -->
	{#if !data.live}
		<span class="min-w-0" data-testid="live"><Plate tone="advisory" icon={Unplug} label={m.shell_reconnecting()} /></span>
	{/if}
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
		<Icon size={18} />
		<span class={collapsed ? 'sr-only' : 'min-w-0 flex-1 truncate'}>{item.label}</span>
		{#if item.count && !collapsed}
			<span class={`rounded-plate px-1.5 font-mono text-xs ${item.alert ? 'bg-warning-soft text-warning-ink' : 'text-ink-3'}`}>{item.count}</span>
		{:else if item.alert}
			<span class="absolute top-2 right-2 size-1.5 rounded-full bg-warning" aria-hidden="true"></span>
		{/if}
	</a>
{/snippet}

{#if wide.current}
	<aside class="fixed top-0 bottom-0 left-0 z-40 flex w-[var(--side-w)] flex-col border-r border-line bg-canvas-deep" data-testid="sidebar" data-collapsed={collapsed}>
		<a class={`flex h-20 items-center gap-2 pt-2 no-underline ${collapsed ? 'justify-center' : 'px-5'}`} href="/" aria-label={m.shell_home()}>
			<img src={asset('favicon.svg')} alt="" width="24" height="24" />
			{#if !collapsed}<span class="text-base font-semibold">mccb</span>{/if}
		</a>
		{#if Extra && !collapsed}<div class="px-4 pb-2"><Extra part="badge" /></div>{/if}
		<nav class={`flex flex-1 flex-col gap-0.5 ${collapsed ? 'items-center' : 'px-3'}`} aria-label={m.shell_nav_label()}>
			{#each NAV as item (item.page)}{@render link(item)}{/each}
			<span class="flex-1"></span>
			{#if (sending.length || !data.live) && !collapsed}<div class="flex flex-wrap items-center gap-2 px-1 pb-1">{@render status()}</div>{/if}
			{@render link(SETTINGS)}
		</nav>
		<div class={`mt-2 flex items-center border-t border-line py-2 ${collapsed ? 'flex-col' : 'gap-1 px-3'}`}>
			<IconButton icon={collapsed ? PanelLeftOpen : PanelLeftClose} label={collapsed ? m.shell_expand() : m.shell_collapse()} aria-expanded={!collapsed} onclick={() => prefs.set('sidebar', collapsed ? 'open' : 'collapsed')} data-testid="sidebar-toggle" />
			{#if !collapsed}<span class="min-w-0 flex-1 truncate font-mono text-xs text-ink-3">{data.version ?? ''}</span>{/if}
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
					</a>
				{/each}
			</nav>
			<span class="flex min-w-0 flex-1 justify-center">{#if Extra}<Extra part="badge" />{/if}</span>
			{@render status()}
			<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
		</div>
	</header>
{/if}

<main id="main" class="px-[var(--gutter)] pt-[var(--bar-h)] pb-28 lg:ml-[var(--side-w)] lg:px-8">
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
</main>

{#if Extra}<Extra part="footer" />{/if}
