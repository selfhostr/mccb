<script lang="ts">
	/**
	 * A mail system or file kind in a small square: a neutral icon, or `mark`, the initial of a named
	 * service (no logo, no brand colour). Decorative: the label beside it says the name.
	 */
	import type { Component } from 'svelte';

	let { icon: Icon, mark, size = 32 }: { icon: Component<{ size?: number }>; mark?: string; size?: number } = $props();
</script>

<span
	class="inline-flex shrink-0 items-center justify-center rounded-btn border border-line-strong bg-surface-2 text-ink"
	style:width={`${size}px`}
	style:height={`${size}px`}
	aria-hidden="true"
	>{#if mark}<span class="text-sm leading-none font-semibold">{mark}</span>{:else}<Icon size={Math.round(size / 2)} />{/if}</span
>
