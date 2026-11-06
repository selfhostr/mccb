<script lang="ts">
	/** Status: always an icon and a word, the colour only repeats them. */
	import type { Component } from 'svelte';

	export type Tone = 'signal' | 'info' | 'advisory' | 'warning' | 'ghost';

	interface Props {
		tone?: Tone;
		icon: Component<{ class?: string; size?: number }>;
		label: string;
		title?: string;
		class?: string;
	}

	let { tone = 'ghost', icon: Icon, label, title, class: className = '' }: Props = $props();

	const TONES: Record<Tone, string> = {
		signal: 'text-signal-ink',
		info: 'text-info-ink',
		advisory: 'text-advisory-ink',
		warning: 'text-warning-ink',
		ghost: 'text-ink-2'
	};
</script>

<span class={`inline-flex max-w-full items-center gap-1.5 text-xs font-semibold whitespace-nowrap ${TONES[tone]} ${className}`} {title}>
	<Icon size={13} />
	<span class="truncate">{label}</span>
</span>
