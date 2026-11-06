<script lang="ts">
	/** A bar that only animates transform: progress never causes layout. */
	interface Props {
		value: number;
		tone?: 'ink' | 'muted' | 'signal' | 'advisory' | 'warning';
		label?: string;
		height?: string;
	}

	let { value, tone = 'ink', label, height = 'h-1' }: Props = $props();
	const TONES = { ink: 'bg-ink', muted: 'bg-ink-3', signal: 'bg-signal', advisory: 'bg-advisory', warning: 'bg-warning' };
	const clamped = $derived(Math.max(0, Math.min(100, value)));
</script>

<div class={`w-full overflow-hidden rounded-plate bg-ghost ${height}`} role="progressbar" aria-valuenow={clamped} aria-valuemin="0" aria-valuemax="100" aria-label={label}>
	<div class={`bar-fill ${TONES[tone]}`} style:transform={`scaleX(${clamped / 100})`}></div>
</div>
