<script lang="ts">
	/** One h1, one plain sentence that says what the data below shows. */
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		sentence: string;
		actions?: Snippet;
	}

	let { title, sentence, actions }: Props = $props();
</script>

<header class="flex flex-wrap items-end justify-between gap-3 pt-6 pb-4">
	<div class="min-w-0">
		<h1 class="text-xl">{title}</h1>
		<p class="mt-1 max-w-[44rem] text-[0.9375rem] text-ink-2" data-testid="page-sentence">{sentence}</p>
	</div>
	{#if actions}<div class="flex shrink-0 flex-wrap items-center gap-2">{@render actions()}</div>{/if}
</header>
