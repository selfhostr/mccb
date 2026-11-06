<script lang="ts">
	/** A button that opens a short list of actions. Secondary actions live here, out of the way. */
	import type { Component } from 'svelte';

	export interface MenuItem {
		label: string;
		icon?: Component<{ class?: string; size?: number }>;
		danger?: boolean;
		disabled?: boolean;
		onselect: () => unknown;
	}
	interface Props {
		label: string;
		items: MenuItem[];
		icon?: Component<{ class?: string; size?: number }>;
		text?: string;
		primary?: boolean;
	}

	let { label, items, icon: Icon, text, primary = false }: Props = $props();
	let open = $state(false);
	let above = $state(false);
	let root = $state<HTMLElement>();

	function toggle() {
		if (!open && root) above = root.getBoundingClientRect().bottom > innerHeight - (items.length * 44 + 24);
		open = !open;
		if (open) setTimeout(() => root?.querySelector<HTMLElement>('[role="menuitem"]')?.focus());
	}

	function close(focus = true) {
		open = false;
		if (focus) root?.querySelector<HTMLElement>('button')?.focus();
	}

	function onkeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.stopPropagation();
			close();
			return;
		}
		if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
		e.preventDefault();
		const all = [...(root?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
		const i = all.indexOf(document.activeElement as HTMLElement);
		all[(i + (e.key === 'ArrowDown' ? 1 : -1) + all.length) % all.length]?.focus();
	}

	function onWindowClick(e: MouseEvent) {
		if (open && !root?.contains(e.target as Node)) close(false);
	}
</script>

<svelte:window onclick={onWindowClick} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div bind:this={root} class="relative inline-block" {onkeydown}>
	<button
		type="button"
		class={`inline-flex min-h-10 min-w-10 items-center justify-center gap-2 rounded-btn transition-[opacity,background-color,border-color,color] duration-200 ${primary ? 'bg-primary px-4 text-sm font-semibold text-on-primary hover:opacity-90' : text ? 'border border-line-strong bg-surface px-4 text-sm font-semibold text-ink hover:border-ink-3' : 'text-ink-2 hover:bg-ghost hover:text-ink'}`}
		aria-haspopup="menu"
		aria-expanded={open}
		aria-label={label}
		title={label}
		onclick={toggle}
	>
		{#if Icon}<Icon size={16} />{/if}
		{#if text}{text}{/if}
	</button>
	{#if open}
		<div class={`absolute right-0 z-40 min-w-44 rounded-btn border border-line-strong bg-surface-2 p-1 shadow-float ${above ? 'bottom-full mb-1' : 'top-full mt-1'}`} role="menu" aria-label={label}>
			{#each items as item (item.label)}
				{@const ItemIcon = item.icon}
				<button
					type="button"
					role="menuitem"
					disabled={item.disabled}
					class={`flex min-h-11 w-full items-center gap-2 rounded-plate px-3 text-left whitespace-nowrap hover:bg-ghost disabled:cursor-not-allowed disabled:opacity-50 ${item.danger ? 'text-warning-ink' : 'text-ink'}`}
					onclick={() => {
						close(false);
						item.onselect();
					}}
				>
					{#if ItemIcon}<ItemIcon size={14} />{/if}
					{item.label}
				</button>
			{/each}
		</div>
	{/if}
</div>
