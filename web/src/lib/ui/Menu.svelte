<script lang="ts">
	/** A button that opens a short list of actions. Secondary actions live here, out of the way. */
	import type { Component } from 'svelte';

	export interface MenuItem {
		label: string;
		icon?: Component<{ class?: string; size?: number }>;
		danger?: boolean;
		disabled?: boolean;
		/** Why the item is disabled: shown under its label and read as its description. */
		reason?: string;
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
	const uid = $props.id();

	const entries = () => [...(root?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];

	function toggle() {
		if (!open && root) above = root.getBoundingClientRect().bottom > innerHeight - (items.length * 44 + 24);
		open = !open;
		if (open) setTimeout(() => (root?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? entries()[0])?.focus());
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
		// Tab leaves the menu: focus goes back to the trigger first, so the browser moves on from there.
		if (e.key === 'Tab') {
			close();
			return;
		}
		const all = entries();
		if (!all.length) return;
		const i = all.indexOf(document.activeElement as HTMLElement);
		const to = { ArrowDown: (i + 1) % all.length, ArrowUp: (i - 1 + all.length) % all.length, Home: 0, End: all.length - 1 }[e.key];
		if (to === undefined) return;
		e.preventDefault();
		all[to]?.focus();
	}

	function choose(item: MenuItem) {
		if (item.disabled) return;
		close();
		item.onselect();
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
			{#each items as item, i (item.label)}
				{@const ItemIcon = item.icon}
				{@const why = item.disabled && item.reason ? `${uid}-why-${i}` : undefined}
				<!-- aria-disabled, not disabled: the item stays reachable, so its reason can be read. -->
				<button
					type="button"
					role="menuitem"
					aria-disabled={item.disabled ? 'true' : undefined}
					aria-describedby={why}
					class={`flex min-h-11 w-full items-center gap-2 rounded-plate px-3 text-left whitespace-nowrap ${item.disabled ? 'cursor-not-allowed' : 'hover:bg-ghost'} ${item.danger ? 'text-warning-ink' : 'text-ink'}`}
					onclick={() => choose(item)}
				>
					{#if ItemIcon}<span class={item.disabled ? 'opacity-50' : ''}><ItemIcon size={14} /></span>{/if}
					<span class="flex min-w-0 flex-col py-1.5">
						<span class={item.disabled ? 'opacity-50' : ''}>{item.label}</span>
						{#if why}<span id={why} class="max-w-64 text-xs whitespace-normal text-ink-3">{item.reason}</span>{/if}
					</span>
				</button>
			{/each}
		</div>
	{/if}
</div>
