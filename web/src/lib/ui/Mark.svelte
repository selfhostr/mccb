<script lang="ts">
	/** A small square with letters in a provider's colour: tells mail systems apart at a glance. Decorative. */
	let { mono, tone, size = 32 }: { mono: string; tone: string; size?: number } = $props();
</script>

<span
	class="inline-flex shrink-0 items-center justify-center rounded-btn font-semibold text-white"
	style:width={`${size}px`}
	style:height={`${size}px`}
	style:background={tone}
	style:font-size={`${Math.max(9, Math.round(size / (mono.length > 2 ? 3.4 : 2.6)))}px`}
	aria-hidden="true">{mono}</span
>
