<script lang="ts">
	let { class: className = '' }: { class?: string } = $props();
</script>

<div class={`skeleton rounded-plate bg-ghost ${className}`} aria-hidden="true"></div>
