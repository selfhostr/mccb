<script lang="ts">
	import FolderUp from '@lucide/svelte/icons/folder-up';
	import LogOut from '@lucide/svelte/icons/log-out';
	import Unplug from '@lucide/svelte/icons/unplug';
	import Wifi from '@lucide/svelte/icons/wifi';
	import type { Component } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { post } from '../lib/api/client.ts';
	import { asset } from '../lib/base.ts';
	import { pct } from '../lib/format.ts';
	import { router } from '../lib/router.svelte.ts';
	import { data } from '../lib/stores/data.svelte.ts';
	import IconButton from '../lib/ui/IconButton.svelte';
	import Plate from '../lib/ui/Plate.svelte';
	import { uploads } from '../lib/upload/store.svelte.ts';
	import FilesPage from './files/FilesPage.svelte';
	import MigrationsPage from './migrations/MigrationsPage.svelte';
	import SettingsPage from './settings/SettingsPage.svelte';

	/** extra: only the static demo passes one (its badge); the product never does. */
	let { onlogout, extra: Extra }: { onlogout: () => void; extra?: Component<{ part: 'badge' | 'footer' }> } = $props();

	const NAV = $derived([
		{ page: 'migrations', href: '/', label: m.shell_nav_migrations() },
		{ page: 'files', href: '/files', label: m.shell_nav_files() },
		{ page: 'settings', href: '/settings', label: m.shell_nav_settings() }
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
		{#if sending.length}
			<a class="inline-flex min-h-10 items-center gap-2 rounded-btn px-2 text-ink-2 no-underline transition-colors duration-200 hover:text-ink" href="/files" data-testid="upload-indicator">
				<FolderUp size={14} />
				<span class="font-mono text-xs">{m.shell_uploading({ percent: sendingPercent })}</span>
			</a>
		{/if}
		<span class="min-w-0" data-testid="live">
			<Plate tone={data.live ? 'ghost' : 'advisory'} icon={data.live ? Wifi : Unplug} label={data.live ? m.shell_live() : m.shell_reconnecting()} class={data.live ? 'font-normal' : ''} />
		</span>
		<IconButton icon={LogOut} label={m.shell_sign_out()} onclick={logout} />
	</div>
</header>

<main id="main" class="mx-auto max-w-[1160px] px-[var(--gutter)] pt-[var(--bar-h)] pb-28">
	{#if router.route.page === 'migrations'}
		<MigrationsPage />
	{:else if router.route.page === 'files'}
		<FilesPage />
	{:else}
		<SettingsPage />
	{/if}
</main>

{#if Extra}<Extra part="footer" />{/if}
