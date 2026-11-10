<script lang="ts">
	/**
	 * One compact settings row: the label on the left, the control on the right. A row whose control is a
	 * switch or a select is a `<label>`, so a click on the name works too; a row with buttons is a `div`.
	 * `help` adds a question mark beside the label, only where the name alone could be misread.
	 */
	import type { Snippet } from 'svelte';
	import Help from './Help.svelte';

	interface Props {
		label: string;
		help?: string;
		as?: 'label' | 'div';
		children: Snippet;
		class?: string;
		[key: string]: unknown;
	}

	let { label, help, as = 'label', children, class: className = '', ...rest }: Props = $props();
</script>

<!-- The control comes first in the document: a label names its first control, and the help mark is a button.
     The order on screen stays label, help, control. -->
<svelte:element this={as} class={`relative flex min-h-12 items-center gap-1 px-4 py-1 ${className}`} {...rest}>
	<span class="order-3 ml-auto flex min-w-0 max-w-[65%] items-center justify-end gap-1 pl-2 [&_select]:w-36 sm:[&_select]:w-44">{@render children()}</span>
	<span class="order-1 min-w-0 text-ink">{label}</span>
	{#if help}<span class="order-2 inline-flex shrink-0"><Help text={help} /></span>{/if}
</svelte:element>
