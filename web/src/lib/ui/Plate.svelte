<script lang="ts">
	/**
	 * Status: always an icon and a word, the colour only repeats them.
	 * `busy`: work in progress, the icon breathes slowly. A plate that turns to `signal` while on
	 * screen pops its icon once, so the moment something finishes is seen; one already done when shown stays still.
	 */
	import type { Component } from 'svelte';

	export type Tone = 'signal' | 'info' | 'advisory' | 'warning' | 'ghost';

	interface Props {
		tone?: Tone;
		icon: Component<{ class?: string; size?: number }>;
		label: string;
		title?: string;
		busy?: boolean;
		class?: string;
	}

	let { tone = 'ghost', icon: Icon, label, title, busy = false, class: className = '' }: Props = $props();

	const TONES: Record<Tone, string> = {
		signal: 'text-signal-ink',
		info: 'text-info-ink',
		advisory: 'text-advisory-ink',
		warning: 'text-warning-ink',
		ghost: 'text-ink-2'
	};

	// The tone it was first shown with: only a change to signal after that is a success worth marking.
	let shown: Tone | undefined;
	let popped = $state(false);
	$effect.pre(() => {
		if (shown !== undefined && shown !== tone && tone === 'signal') popped = true;
		else if (tone !== 'signal') popped = false;
		shown = tone;
	});
</script>

<span class={`inline-flex max-w-full items-center gap-1.5 text-xs font-semibold whitespace-nowrap ${TONES[tone]} ${className}`} {title}>
	<span class={`inline-flex ${busy ? 'busy' : popped ? 'done-pop' : ''}`}><Icon size={14} /></span>
	<span class="truncate">{label}</span>
</span>
