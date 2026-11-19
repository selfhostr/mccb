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

<div class="appear flex flex-col items-center gap-2 px-4 py-12 text-center" data-testid="empty-state">
	<!-- The icon settles in once, a beat after the text: the eye lands on it, then reads. -->
	<span class="empty-mark mb-1 inline-flex size-10 items-center justify-center rounded-full bg-ghost text-ink-3"><Icon size={20} /></span>
	<h2 class="text-base">{title}</h2>
	{#if description}<p class="max-w-[32rem] text-ink-2">{description}</p>{/if}
	{#if action}<div class="mt-2 flex flex-wrap justify-center gap-2">{@render action()}</div>{/if}
</div>
