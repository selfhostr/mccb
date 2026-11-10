<script lang="ts">
	/** One group of the settings page: a small title, then one bordered list of rows. */
	import type { Snippet } from 'svelte';

	interface Props {
		id: string;
		title: string;
		children: Snippet;
	}

	let { id, title, children }: Props = $props();
</script>

<section {id} class="flex flex-col gap-2" data-testid={`section-${id}`} aria-labelledby={`${id}-title`}>
	<h2 id={`${id}-title`} class="text-sm font-semibold">{title}</h2>
	<div class="divide-y divide-line rounded-card border border-line-strong bg-surface">{@render children()}</div>
</section>
