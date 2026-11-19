<script lang="ts">
	/**
	 * Tabs with roving focus: arrows, Home and End move between them, Tab goes on to the content.
	 * Give the content as `children` and it is rendered as the labelled tab panel; when the content
	 * lives elsewhere (a page of its own), pass that element's id as `panel` for `aria-controls`.
	 */
	import type { Snippet } from 'svelte';

	interface Tab {
		id: string;
		label: string;
	}
	interface Props {
		tabs: Tab[];
		value: string;
		label: string;
		panel?: string;
		children?: Snippet;
		onchange: (id: string) => void;
	}

	let { tabs, value, label, panel, children, onchange }: Props = $props();
	let list = $state<HTMLElement>();
	const uid = $props.id();
	const tabId = (id: string) => `${uid}-tab-${id}`;
	const panelId = $derived(children ? `${uid}-panel` : panel);

	function onkeydown(e: KeyboardEvent) {
		if (!tabs.length) return;
		const i = tabs.findIndex((t) => t.id === value);
		const keys: Record<string, number> = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1 };
		const to = keys[e.key];
		if (to === undefined) return;
		e.preventDefault();
		const next = tabs[to];
		if (next.id !== value) onchange(next.id);
		list?.querySelector<HTMLElement>(`[data-tab="${next.id}"]`)?.focus();
	}
</script>

<div bind:this={list} class="flex gap-1 border-b border-line-strong" role="tablist" aria-label={label} tabindex="-1" {onkeydown}>
	{#each tabs as tab (tab.id)}
		<button
			type="button"
			role="tab"
			id={tabId(tab.id)}
			data-tab={tab.id}
			aria-selected={tab.id === value}
			aria-controls={tab.id === value ? panelId : undefined}
			tabindex={tab.id === value ? 0 : -1}
			class={`-mb-px min-h-10 border-b-2 px-3 font-medium transition-colors duration-200 ${tab.id === value ? 'border-ink text-ink' : 'border-transparent text-ink-2 hover:text-ink'}`}
			onclick={() => onchange(tab.id)}
		>
			{tab.label}
		</button>
	{/each}
</div>
{#if children}
	<div id={panelId} role="tabpanel" aria-labelledby={tabId(value)} tabindex="0">
		{@render children()}
	</div>
{/if}
