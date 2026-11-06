<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		children: Snippet;
		class?: string;
	}

	let { children, class: className = '' }: Props = $props();
</script>

<div class={`rounded-card border border-line-strong bg-surface ${className}`}>
	{@render children()}
</div>
