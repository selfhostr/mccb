<script lang="ts">
	/** Limits and warnings: advisory tint, icon, one or two sentences. tone "note" is a plain explanation, without the tint. */
	import Info from '@lucide/svelte/icons/info';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import type { Snippet } from 'svelte';

	let { tone = 'advisory', children }: { tone?: 'advisory' | 'note'; children: Snippet } = $props();
	const Icon = $derived(tone === 'note' ? Info : TriangleAlert);
</script>

<div class={`flex items-start gap-2 rounded-btn border p-3 ${tone === 'note' ? 'border-line bg-surface text-ink-2' : 'border-advisory/30 bg-advisory-soft text-advisory-ink'}`}>
	<span class="mt-0.5"><Icon size={14} /></span>
	<div class="min-w-0">{@render children()}</div>
</div>
