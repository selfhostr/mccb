<script lang="ts">
	/** Status chip: always an icon and a word, the colour only repeats them. */
	import type { Component } from 'svelte';

	export type Tone = 'signal' | 'info' | 'advisory' | 'warning' | 'ghost';

	interface Props {
		tone?: Tone;
		icon: Component<{ class?: string; size?: number }>;
		label: string;
		spin?: boolean;
		title?: string;
		class?: string;
	}

	let { tone = 'ghost', icon: Icon, label, spin = false, title, class: className = '' }: Props = $props();

	const TONES: Record<Tone, string> = {
		signal: 'bg-signal-soft text-signal-ink border-signal/30',
		info: 'bg-info-soft text-info-ink border-info/30',
		advisory: 'bg-advisory-soft text-advisory-ink border-advisory/30',
		warning: 'bg-warning-soft text-warning-ink border-warning/30',
		ghost: 'bg-ghost text-ink-2 border-line'
	};
</script>

<span class={`inline-flex min-h-[22px] max-w-full items-center gap-1 rounded-plate border px-1.5 text-xs font-semibold whitespace-nowrap ${TONES[tone]} ${className}`} {title}>
	<Icon size={13} class={spin ? 'spin' : ''} />
	<span class="truncate">{label}</span>
</span>
