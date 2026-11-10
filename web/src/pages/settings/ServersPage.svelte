<script lang="ts">
	/**
	 * Every server in one list and one way to add one: IMAP servers, Microsoft 365 tenants, Exchange,
	 * JMAP. The rows come from two stores, hence the two list components in the same frame.
	 */
	import Plus from '@lucide/svelte/icons/plus';
	import ServerIcon from '@lucide/svelte/icons/server';
	import { m } from '#lib/paraglide/messages.js';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import AddServer from './AddServer.svelte';
	import ServersSection from './ServersSection.svelte';
	import SourcesSection from './SourcesSection.svelte';

	let adding = $state(false);
	let servers = $state<ServersSection>();
	let connections = $state<SourcesSection>();
	const total = $derived(data.servers.length + sources.list.length);
	void sources.load();
</script>

<PageHeader title={m.settings_servers_title()}>
	{#snippet actions()}
		<IconButton variant="primary" icon={Plus} label={m.settings_servers_add()} onclick={() => (adding = true)} data-testid="server-add" />
	{/snippet}
</PageHeader>

<div class="max-w-[1040px]">
	{#if total}
		<Panel>
			<ServersSection bind:this={servers} />
			<SourcesSection bind:this={connections} />
		</Panel>
	{:else}
		<EmptyState icon={ServerIcon} title={m.settings_servers_empty_title()} />
	{/if}
</div>

{#if adding}
	<AddServer onclose={() => (adding = false)} onsaved={(saved) => servers?.test(saved)} onconnection={(saved) => connections?.check(saved)} />
{/if}
