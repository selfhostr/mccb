<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		hint?: string;
		error?: string | null;
		children: Snippet;
		class?: string;
	}

	let { label, hint, error, children, class: className = '' }: Props = $props();
</script>

<label class={`flex min-w-0 flex-col gap-1 ${className}`}>
	<span class="font-medium text-ink-2">{label}</span>
	{@render children()}
	{#if hint}<small class="text-xs text-ink-3">{hint}</small>{/if}
	{#if error}<small class="text-xs text-warning-ink" role="alert">{error}</small>{/if}
</label>
