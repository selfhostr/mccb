<script lang="ts">
	/** One block of a settings page: a heading, one sentence, then its controls. */
	import type { Snippet } from 'svelte';

	interface Props {
		id: string;
		title: string;
		sentence: string;
		actions?: Snippet;
		children: Snippet;
		/** The page header already names it: only the actions show. */
		bare?: boolean;
	}

	let { id, title, sentence, actions, children, bare = false }: Props = $props();
</script>

<section {id} class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid={`section-${id}`}>
	<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
		<div class={`min-w-0 flex-1 basis-56 ${bare ? 'sr-only' : ''}`}>
			<h2 class="text-base">{title}</h2>
			<p class="m-0 mt-1 text-ink-2">{sentence}</p>
		</div>
		{#if actions}{@render actions()}{/if}
	</div>
	{@render children()}
</section>
