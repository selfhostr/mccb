<script lang="ts">
	/**
	 * A quiet "more options" row. The summary shows a chevron: a flex summary loses the
	 * browser marker, and without it a closed disclosure looks like a heading with nothing under it.
	 */
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		/** Short state shown next to the title, e.g. "On". */
		summary?: string;
		open?: boolean;
		children: Snippet;
		class?: string;
		[key: string]: unknown;
	}

	let { title, summary, open = $bindable(false), children, class: className = '', ...rest }: Props = $props();
</script>

<details class={`group ${className}`} bind:open {...rest}>
	<summary class="flex min-h-10 cursor-pointer list-none items-center gap-2 font-medium text-ink-2 hover:text-ink [&::-webkit-details-marker]:hidden">
		<ChevronRight size={16} class="shrink-0 transition-transform duration-200 group-open:rotate-90 rtl:rotate-180 rtl:group-open:rotate-90" />
		<span>{title}</span>
		{#if summary}<span class="text-xs font-normal text-ink-3">{summary}</span>{/if}
	</summary>
	<div class="pb-2 pl-6">{@render children()}</div>
</details>
