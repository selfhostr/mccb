<script lang="ts">
	/** Limits and warnings: advisory tint, icon, one or two sentences. */
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<div class="flex items-start gap-2 rounded-btn border border-advisory/30 bg-advisory-soft p-2.5 text-advisory-ink">
	<span class="mt-0.5"><TriangleAlert size={14} /></span>
	<div class="min-w-0">{@render children()}</div>
</div>
