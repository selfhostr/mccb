<script lang="ts">
	/** A mail system or file kind as a neutral icon in a small square. Decorative: the label says the name. */
	import type { Component } from 'svelte';

	let { icon: Icon, size = 32 }: { icon: Component<{ size?: number }>; size?: number } = $props();
</script>

<span
	class="inline-flex shrink-0 items-center justify-center rounded-btn border border-line-strong bg-surface-2 text-ink"
	style:width={`${size}px`}
	style:height={`${size}px`}
	aria-hidden="true"><Icon size={Math.round(size / 2)} /></span
>
