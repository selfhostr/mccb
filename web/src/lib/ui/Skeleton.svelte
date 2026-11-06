<script lang="ts">
	let { class: className = '' }: { class?: string } = $props();
</script>

<div class={`shimmer rounded-plate bg-ghost ${className}`} aria-hidden="true"></div>
