<script lang="ts">
	import type { Component, Snippet } from 'svelte';

	interface Props {
		icon: Component<{ class?: string; size?: number }>;
		title: string;
		description?: string;
		action?: Snippet;
	}

	let { icon: Icon, title, description, action }: Props = $props();
</script>

<div class="flex flex-col items-center gap-2 px-4 py-12 text-center" data-testid="empty-state">
	<span class="mb-1 text-ink-3"><Icon size={24} /></span>
	<h2 class="text-base">{title}</h2>
	{#if description}<p class="max-w-[32rem] text-ink-2">{description}</p>{/if}
	{#if action}<div class="mt-2 flex flex-wrap justify-center gap-2">{@render action()}</div>{/if}
</div>
