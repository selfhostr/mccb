<script lang="ts">
	/**
	 * A 40 px square with a named icon. `label` is the accessible name and the tooltip.
	 * `variant`: `primary` for the one thing a page adds, `outline` for a second action beside it.
	 */
	import Hourglass from '@lucide/svelte/icons/hourglass';
	import type { Component } from 'svelte';

	interface Props {
		icon: Component<{ class?: string; size?: number }>;
		label: string;
		disabled?: boolean;
		loading?: boolean;
		tone?: 'plain' | 'danger';
		variant?: 'plain' | 'primary' | 'outline';
		href?: string;
		class?: string;
		onclick?: (e: MouseEvent) => unknown;
		[key: string]: unknown;
	}

	let { icon: Icon, label, disabled = false, loading = false, tone = 'plain', variant = 'plain', href, class: className = '', onclick, ...rest }: Props = $props();

	const LOOK = {
		plain: 'hover:bg-ghost',
		primary: 'bg-primary text-on-primary hover:opacity-90',
		outline: 'border border-line-strong bg-surface hover:border-ink-3'
	};
	const ink = $derived(variant === 'primary' ? '' : tone === 'danger' ? 'text-warning-ink' : variant === 'outline' ? 'text-ink' : 'text-ink-2 hover:text-ink');
	const classes = $derived(`inline-flex size-10 shrink-0 items-center justify-center rounded-btn transition-[opacity,background-color,border-color,color] duration-200 disabled:cursor-not-allowed disabled:opacity-50 ${LOOK[variant]} ${ink} ${className}`);
</script>

{#if href && !disabled}
	<a {href} class={classes} aria-label={label} title={label} {...rest}><Icon size={16} /></a>
{:else}
	<button type="button" class={classes} aria-label={label} title={label} aria-busy={loading} disabled={disabled || loading} {onclick} {...rest}>
		{#if loading}<Hourglass size={16} />{:else}<Icon size={16} />{/if}
	</button>
{/if}
