<script lang="ts">
	/** A 40 px square with a named icon. `label` is the accessible name and the tooltip. */
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import type { Component } from 'svelte';

	interface Props {
		icon: Component<{ class?: string; size?: number }>;
		label: string;
		disabled?: boolean;
		loading?: boolean;
		tone?: 'plain' | 'danger';
		class?: string;
		onclick?: (e: MouseEvent) => unknown;
		[key: string]: unknown;
	}

	let { icon: Icon, label, disabled = false, loading = false, tone = 'plain', class: className = '', onclick, ...rest }: Props = $props();
</script>

<button
	type="button"
	class={`inline-flex size-10 shrink-0 items-center justify-center rounded-btn transition-colors duration-200 hover:bg-ghost disabled:cursor-not-allowed disabled:opacity-50 ${tone === 'danger' ? 'text-warning-ink' : 'text-ink-2 hover:text-ink'} ${className}`}
	aria-label={label}
	title={label}
	aria-busy={loading}
	disabled={disabled || loading}
	{onclick}
	{...rest}
>
	{#if loading}<LoaderCircle size={16} class="spin" />{:else}<Icon size={16} />{/if}
</button>
