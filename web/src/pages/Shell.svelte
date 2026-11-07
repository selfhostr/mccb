<script lang="ts">
	import ArrowRightLeft from '@lucide/svelte/icons/arrow-right-left';
	import FolderOpen from '@lucide/svelte/icons/folder-open';
	import FolderUp from '@lucide/svelte/icons/folder-up';
	import LogOut from '@lucide/svelte/icons/log-out';
	import Server from '@lucide/svelte/icons/server';
	import Settings from '@lucide/svelte/icons/settings';
	import Unplug from '@lucide/svelte/icons/unplug';
	import Wifi from '@lucide/svelte/icons/wifi';
	import type { Component } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { post } from '../lib/api/client.ts';
	import { asset } from '../lib/base.ts';
	import { pct } from '../lib/format.ts';
	import { summarize } from '../lib/logic/accounts.ts';
	import { router } from '../lib/router.svelte.ts';
	import { data } from '../lib/stores/data.svelte.ts';
	import IconButton from '../lib/ui/IconButton.svelte';
	import Plate from '../lib/ui/Plate.svelte';
	import { uploads } from '../lib/upload/store.svelte.ts';
	import FilesPage from './files/FilesPage.svelte';
	import MigrationsPage from './migrations/MigrationsPage.svelte';
	import ServersPage from './settings/ServersPage.svelte';
	import SettingsPage from './settings/SettingsPage.svelte';

	/** extra: only the static demo passes one (its badge); the product never does. */
	let { onlogout, extra: Extra }: { onlogout: () => void; extra?: Component<{ part: 'badge' | 'footer' }> } = $props();

	// Same width as --side-w in tokens.css: a column on the left, else a bar on top. Only one is in the page.
	const wide = new MediaQuery('min-width: 64rem');

	// What each page holds, said next to its link: what needs you, and how many files.
	const attention = $derived(summarize(data.accounts.values(), (id) => data.jobs.get(id)).attention);
	const NAV = $derived([
		{ page: 'migrations', href: '/', label: m.shell_nav_migrations(), icon: ArrowRightLeft, count: attention, alert: attention > 0 },
		{ page: 'files', href: '/files', label: m.shell_nav_files(), icon: FolderOpen, count: data.psts.size, alert: false },
		{ page: 'servers', href: '/servers', label: m.settings_servers_title(), icon: Server, count: data.servers.length, alert: false },
		{ page: 'settings', href: '/settings', label: m.shell_nav_settings(), icon: Settings, count: 0, alert: false }
	]);

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
	<span class="min-w-0" data-testid="live">
		<Plate tone={data.live ? 'ghost' : 'advisory'} icon={data.live ? Wifi : Unplug} label={data.live ? m.shell_live() : m.shell_reconnecting()} class={data.live ? 'font-normal' : ''} />
	</span>
{/snippet}

{#if wide.current}
	<aside class="fixed top-0 bottom-0 left-0 z-40 flex w-[var(--side-w)] flex-col border-r border-line bg-canvas-deep" data-testid="sidebar">
		<a class="flex min-h-16 items-center gap-2 px-5 no-underline" href="/" aria-label={m.shell_home()}>
			<img src={asset('favicon.svg')} alt="" width="28" height="28" />
			<span class="text-[0.9375rem] font-semibold">mccb</span>
		</a>
		{#if Extra}<div class="px-4 pb-2"><Extra part="badge" /></div>{/if}
		<nav class="flex flex-col gap-0.5 px-3" aria-label={m.shell_nav_label()}>
			{#each NAV as item (item.page)}
				{@const Icon = item.icon}
				{@const current = router.route.page === item.page}
				<a
					class={`flex min-h-10 items-center gap-3 rounded-btn px-3 no-underline transition-colors duration-200 ${current ? 'bg-surface-2 font-medium text-ink' : 'text-ink-2 hover:bg-ghost hover:text-ink'}`}
					href={item.href}
					aria-current={current ? 'page' : undefined}
				>
					<Icon size={17} />
					<span class="min-w-0 flex-1 truncate">{item.label}</span>
					{#if item.count}
						<span class={`rounded-plate px-1.5 font-mono text-xs ${item.alert ? 'bg-warning-soft text-warning-ink' : 'text-ink-3'}`}>{item.count}</span>
					{/if}
				</a>
			{/each}
		</nav>
		<div class="mt-auto flex flex-col gap-1 border-t border-line p-3">
			<div class="flex flex-wrap items-center gap-2">{@render status()}</div>
			<div class="flex items-center justify-between gap-2">
				<span class="truncate font-mono text-xs text-ink-3">{data.version ?? ''}</span>
				<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
			</div>
		</div>
	</aside>
{:else}
	<header class="fixed inset-x-0 top-0 z-40 h-[var(--bar-h)] border-b border-line bg-canvas">
		<div class="flex h-full flex-wrap content-center items-center gap-x-3 px-[var(--gutter)]">
			<a class="flex min-h-10 items-center gap-2 no-underline" href="/" aria-label={m.shell_home()}>
				<img src={asset('favicon.svg')} alt="" width="28" height="28" />
				<span class="text-[0.9375rem] font-semibold">mccb</span>
			</a>
			<nav class="order-last flex w-full gap-0.5 sm:order-none sm:ml-2 sm:w-auto" aria-label={m.shell_nav_label()}>
				{#each NAV as item (item.page)}
					<a
						class={`inline-flex min-h-10 items-center rounded-btn px-3 no-underline transition-colors duration-200 ${router.route.page === item.page ? 'bg-surface-2 font-medium text-ink' : 'text-ink-2 hover:text-ink'}`}
						href={item.href}
						aria-current={router.route.page === item.page ? 'page' : undefined}
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
	{:else}
		<SettingsPage />
	{/if}
</main>

{#if Extra}<Extra part="footer" />{/if}
