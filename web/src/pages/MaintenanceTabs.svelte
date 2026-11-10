<script lang="ts">
	/** Repair and Duplicates share one entry of the menu: two tabs, each with its own URL. */
	import { m } from '#lib/paraglide/messages.js';
	import { router } from '../lib/router.svelte.ts';
	import Tabs from '../lib/ui/Tabs.svelte';

	const tabs = $derived([
		{ id: 'repair', label: m.shell_nav_repair() },
		{ id: 'duplicates', label: m.shell_nav_duplicates() }
	]);
</script>

<div data-testid="maintenance-tabs">
	<Tabs {tabs} value={router.route.page} label={m.shell_nav_maintenance()} onchange={(id) => router.go(`/${id}`)} />
</div>
