<script lang="ts">
	/** What failed, in words, an explanation when there is one, the server's message as details, and a way out. */
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import { m } from '#lib/paraglide/messages.js';
	import Button from './Button.svelte';

	interface Props {
		title: string;
		/** A translated sentence shown above the details. */
		summary?: string | null;
		message?: string | null;
		onretry?: () => unknown;
	}

	let { title, summary, message, onretry }: Props = $props();
</script>

<div class="flex flex-wrap items-start gap-3 rounded-card border border-warning/30 bg-warning-soft p-3" role="alert">
	<span class="mt-0.5 text-warning-ink"><CircleAlert size={16} /></span>
	<div class="min-w-0 flex-1 basis-48">
		<p class="m-0 font-semibold">{title}</p>
		{#if summary}<p class="m-0 mt-1 text-sm" data-testid="error-summary">{summary}</p>{/if}
		{#if message}<p class="m-0 mt-1 font-mono text-xs text-ink-2" data-testid="error-details">{message}</p>{/if}
	</div>
	{#if onretry}<Button icon={RotateCw} onclick={onretry}>{m.common_retry()}</Button>{/if}
</div>
