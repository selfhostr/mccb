<script lang="ts">
	/** Tabs with roving focus: arrows move between them, Tab goes on to the content. */
	interface Tab {
		id: string;
		label: string;
	}
	interface Props {
		tabs: Tab[];
		value: string;
		label: string;
		onchange: (id: string) => void;
	}

	let { tabs, value, label, onchange }: Props = $props();
	let list = $state<HTMLElement>();

	function onkeydown(e: KeyboardEvent) {
		const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
		if (!step) return;
		e.preventDefault();
		const i = tabs.findIndex((t) => t.id === value);
		const next = tabs[(i + step + tabs.length) % tabs.length];
		onchange(next.id);
		list?.querySelector<HTMLElement>(`[data-tab="${next.id}"]`)?.focus();
	}
</script>

<div bind:this={list} class="flex gap-1 border-b border-line-strong" role="tablist" aria-label={label} tabindex="-1" {onkeydown}>
	{#each tabs as tab (tab.id)}
		<button
			type="button"
			role="tab"
			data-tab={tab.id}
			aria-selected={tab.id === value}
			tabindex={tab.id === value ? 0 : -1}
			class={`-mb-px min-h-10 border-b-2 px-3 font-medium transition-colors duration-200 ${tab.id === value ? 'border-ink text-ink' : 'border-transparent text-ink-2 hover:text-ink'}`}
			onclick={() => onchange(tab.id)}
		>
			{tab.label}
		</button>
	{/each}
</div>
