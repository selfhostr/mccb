<script lang="ts">
	/** The panel of a mailbox that has no job yet. It stays this panel if a job starts, with a link to it. */
	import X from '@lucide/svelte/icons/x';
	import { onMount, type Snippet } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { rowHref } from '../../lib/logic/routes.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';

	let { id, onclose, settings }: { id: number; onclose: () => void; settings: Snippet<[number]> } = $props();

	let heading = $state<HTMLElement>();
	const email = $derived(prefs.email(data.accounts.get(id)?.email));
	const jobId = $derived(data.jobs.get(id)?.id);

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && !document.querySelector('dialog[open]')) onclose();
	}

	onMount(() => heading?.focus());
</script>

<svelte:window {onkeydown} />

<div class="flex items-center justify-between gap-2 border-b border-line py-2 pr-2 pl-4">
	<h2 bind:this={heading} tabindex="-1" class="min-w-0 truncate text-sm outline-none" data-testid="job-title">{email}</h2>
	<IconButton icon={X} label={m.jobs_panel_close()} onclick={onclose} data-testid="job-close" />
</div>
<div class="min-h-0 flex-1 overflow-y-auto p-4" data-testid="account-settings">
	{@render settings(id)}
	{#if jobId != null}
		<div class="mt-4"><Button href={rowHref(router.search, id, jobId)}>{m.migrations_row_open_details()}</Button></div>
	{/if}
</div>
