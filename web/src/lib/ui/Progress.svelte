<script lang="ts">
	/** A bar that only animates transform: progress never causes layout. */
	interface Props {
		value: number;
		tone?: 'ink' | 'advisory' | 'warning';
		label?: string;
	}

	let { value, tone = 'ink', label }: Props = $props();
	const TONES = { ink: 'bg-ink', advisory: 'bg-advisory', warning: 'bg-warning' };
	const clamped = $derived(Math.max(0, Math.min(100, value)));
</script>

<div class="h-1.5 w-full overflow-hidden rounded-plate bg-line" role="progressbar" aria-valuenow={clamped} aria-valuemin="0" aria-valuemax="100" aria-label={label}>
	<div class={`bar-fill ${TONES[tone]}`} style:transform={`scaleX(${clamped / 100})`}></div>
</div>
