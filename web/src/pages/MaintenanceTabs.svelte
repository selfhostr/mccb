<script lang="ts">
	/**
	 * Repair and Duplicates share one entry of the menu: two tabs, each with its own URL.
	 * Under them, the three steps of both tabs, quiet: analyse, check, then act.
	 */
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import { m } from '#lib/paraglide/messages.js';
	import { router } from '../lib/router.svelte.ts';
	import Tabs from '../lib/ui/Tabs.svelte';

	const tabs = $derived([
		{ id: 'repair', label: m.shell_nav_repair() },
		{ id: 'duplicates', label: m.shell_nav_duplicates() }
	]);
	const steps = $derived([m.maintenance_flow_scan(), m.maintenance_flow_check(), router.route.page === 'duplicates' ? m.maintenance_flow_dedup() : m.maintenance_flow_repair()]);
</script>

<div class="flex flex-col gap-2" data-testid="maintenance-tabs">
	<Tabs {tabs} value={router.route.page} label={m.shell_nav_maintenance()} onchange={(id) => router.go(`/${id}`)} />
	<ol class="m-0 flex list-none flex-wrap items-center gap-x-1 gap-y-0 p-0 text-xs text-ink-3" aria-label={m.maintenance_flow_label()} data-testid="maintenance-flow">
		{#each steps as step, i (i)}
			<li class="inline-flex items-center gap-1">
				{#if i > 0}<ChevronRight size={14} aria-hidden="true" class="rtl:rotate-180" />{/if}
				<span>{i + 1}. {step}</span>
			</li>
		{/each}
	</ol>
</div>
