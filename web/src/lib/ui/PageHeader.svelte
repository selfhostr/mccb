<script lang="ts">
	/** One h1 and, on its right, what can be done on the page. `sentence` only for a count worth reading. */
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		sentence?: string;
		actions?: Snippet;
	}

	let { title, sentence, actions }: Props = $props();
</script>

<header class="flex min-h-20 flex-wrap items-center justify-between gap-3 pt-6 pb-4">
	<div class="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3">
		<h1 class="text-xl">{title}</h1>
		{#if sentence}<p class="m-0 min-w-0 truncate text-sm text-ink-2" data-testid="page-sentence">{sentence}</p>{/if}
	</div>
	{#if actions}<div class="flex shrink-0 flex-wrap items-center gap-2">{@render actions()}</div>{/if}
</header>
