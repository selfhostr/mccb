<script lang="ts">
	import FolderUp from '@lucide/svelte/icons/folder-up';
	import LogOut from '@lucide/svelte/icons/log-out';
	import Settings from '@lucide/svelte/icons/settings';
	import Unplug from '@lucide/svelte/icons/unplug';
	import Waypoints from '@lucide/svelte/icons/waypoints';
	import Wifi from '@lucide/svelte/icons/wifi';
	import { m } from '#lib/paraglide/messages.js';
	import { post } from '../lib/api/client.ts';
	import { pct } from '../lib/format.ts';
	import { router } from '../lib/router.svelte.ts';
	import { data } from '../lib/stores/data.svelte.ts';
	import IconButton from '../lib/ui/IconButton.svelte';
	import Plate from '../lib/ui/Plate.svelte';
	import { uploads } from '../lib/upload/store.svelte.ts';
	import FilesPage from './files/FilesPage.svelte';
	import MigrationsPage from './migrations/MigrationsPage.svelte';
	import SettingsPage from './settings/SettingsPage.svelte';

	let { onlogout }: { onlogout: () => void } = $props();

	const NAV = $derived([
		{ page: 'migrations', href: '/', icon: Waypoints, label: m.shell_nav_migrations() },
		{ page: 'files', href: '/files', icon: FolderUp, label: m.shell_nav_files() },
		{ page: 'settings', href: '/settings', icon: Settings, label: m.shell_nav_settings() }
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

<header class="fixed inset-x-0 top-0 z-40 h-[var(--bar-h)] border-b border-line-strong bg-canvas">
	<div class="mx-auto flex h-full max-w-[1400px] items-center gap-3 px-[var(--gutter)]">
		<a class="flex min-h-10 items-center gap-2 font-semibold no-underline" href="/" aria-label={m.shell_home()}>
			<img src="/favicon.svg" alt="" width="24" height="24" />
			<span class="text-sm">mccb</span>
		</a>
		<nav class="ml-3 hidden items-center gap-1 sm:flex" aria-label={m.shell_nav_label()}>
			{#each NAV as item (item.page)}
				{@const Icon = item.icon}
				<a
					class={`inline-flex min-h-10 items-center gap-2 rounded-btn px-3 font-medium no-underline transition-colors duration-200 ${router.route.page === item.page ? 'bg-ghost text-ink' : 'text-ink-2 hover:text-ink'}`}
					href={item.href}
					aria-current={router.route.page === item.page ? 'page' : undefined}
				>
					<Icon size={15} />{item.label}
				</a>
			{/each}
		</nav>
		<span class="flex-1"></span>
		{#if sending.length}
			<a class="inline-flex min-h-10 items-center gap-2 rounded-btn px-2 text-ink-2 no-underline hover:text-ink" href="/files" data-testid="upload-indicator">
				<FolderUp size={14} />
				<span class="font-mono text-xs">{m.shell_uploading({ percent: sendingPercent })}</span>
			</a>
		{/if}
		<span class="min-w-0" data-testid="live">
			<Plate tone={data.live ? 'signal' : 'advisory'} icon={data.live ? Wifi : Unplug} label={data.live ? m.shell_live() : m.shell_reconnecting()} />
		</span>
		<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
	</div>
</header>

<main id="main" class="mx-auto max-w-[1400px] px-[var(--gutter)] pt-[var(--bar-h)] pb-[calc(var(--tab-h)+112px)] sm:pb-28">
	{#if router.route.page === 'migrations'}
		<MigrationsPage />
	{:else if router.route.page === 'files'}
		<FilesPage />
	{:else}
		<SettingsPage />
	{/if}
</main>

<nav class="fixed inset-x-0 bottom-0 z-30 grid h-[var(--tab-h)] grid-cols-3 border-t border-line-strong bg-canvas sm:hidden" aria-label={m.shell_nav_label()}>
	{#each NAV as item (item.page)}
		{@const Icon = item.icon}
		<a class={`flex flex-col items-center justify-center gap-0.5 text-xs no-underline ${router.route.page === item.page ? 'text-ink' : 'text-ink-3'}`} href={item.href} aria-current={router.route.page === item.page ? 'page' : undefined}>
			<Icon size={18} />{item.label}
		</a>
	{/each}
</nav>
