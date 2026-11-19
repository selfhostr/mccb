<script lang="ts">
	/** Calendars and contacts are asked for but the server has DAV off: said before the start, with the one click that fixes it. */
	import CalendarCheck from '@lucide/svelte/icons/calendar-check';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account, Server } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	interface Props {
		server: Server;
		/** The mailboxes concerned, when several are at stake. */
		accounts?: Account[];
	}

	const SHOWN = 5;

	let { server, accounts = [] }: Props = $props();
	let busy = $state(false);

	async function enable() {
		busy = true;
		try {
			await data.enableDav(server);
		} finally {
			busy = false;
		}
	}
</script>

<Notice>
	<p class="m-0" data-testid="nodav-notice">{m.migrations_import_nodav({ server: server.name })}</p>
	{#if accounts.length}
		<p class="m-0 mt-1 font-mono text-xs" data-testid="import-nodav-list">
			{accounts.slice(0, SHOWN).map((a) => prefs.email(a.email)).join(', ')}{#if accounts.length > SHOWN}<span class="font-sans">{' '}{m.migrations_import_more({ count: accounts.length - SHOWN })}</span>{/if}
		</p>
	{/if}
	<div class="mt-2"><Button icon={CalendarCheck} loading={busy} onclick={enable} data-testid="nodav-enable">{m.migrations_import_nodav_enable()}</Button></div>
</Notice>
