<script lang="ts">
	/**
	 * The one button. `primary` is neutral: one per view, the next thing to do.
	 * `loading` keeps the width and disables the button; a link when `href` is given.
	 */
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import type { Component, Snippet } from 'svelte';

	interface Props {
		children: Snippet;
		variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
		icon?: Component<{ class?: string; size?: number }>;
		href?: string;
		type?: 'button' | 'submit';
		disabled?: boolean;
		loading?: boolean;
		class?: string;
		onclick?: (e: MouseEvent) => unknown;
		[key: string]: unknown;
	}

	let { children, variant = 'secondary', icon: Icon, href, type = 'button', disabled = false, loading = false, class: className = '', onclick, ...rest }: Props = $props();

	const VARIANTS = {
		primary: 'bg-primary text-on-primary hover:opacity-90',
		secondary: 'border border-line-strong bg-surface text-ink hover:border-ink-3',
		ghost: 'text-ink-2 hover:bg-ghost hover:text-ink',
		danger: 'border border-warning/50 bg-surface text-warning-ink hover:bg-warning-soft'
	};
	const classes = $derived(
		`inline-flex min-h-10 min-w-10 select-none items-center justify-center gap-2 rounded-btn px-4 text-sm font-semibold whitespace-nowrap no-underline transition-[opacity,background-color,border-color,color] duration-200 disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`
	);
</script>

{#if href && !disabled}
	<a {href} class={classes} {...rest}>
		{#if Icon}<Icon size={16} />{/if}
		{@render children()}
	</a>
{:else}
	<button {type} class={classes} disabled={disabled || loading} aria-busy={loading} {onclick} {...rest}>
		{#if loading}<LoaderCircle size={16} class="spin" />{:else if Icon}<Icon size={16} />{/if}
		{@render children()}
	</button>
{/if}
