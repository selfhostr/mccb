<script lang="ts">
	/** The servers mailboxes are copied into: their own page, as the first thing to set up. */
	import { m } from '#lib/paraglide/messages.js';
	import { data } from '../../lib/stores/data.svelte.ts';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import ServersSection from './ServersSection.svelte';
</script>

<PageHeader title={m.settings_servers_title()} sentence={data.servers.length ? m.settings_page_sentence({ servers: data.servers.length }) : m.settings_servers_sentence()} />

<div class="max-w-[1040px]">
	<ServersSection />
</div>
