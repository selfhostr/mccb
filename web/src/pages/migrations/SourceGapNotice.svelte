<script lang="ts">
	/**
	 * Calendars or contacts are asked for but the source cannot bring them: said before the start, with the fix.
	 * An IMAP source reads the CalDAV/CardDAV of its saved server: turning DAV on there fixes all its mailboxes at once.
	 */
	import CalendarCheck from '@lucide/svelte/icons/calendar-check';
	import { m } from '#lib/paraglide/messages.js';
	import type { GapGroup } from '../../lib/logic/sourceDav.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	interface Props {
		groups: GapGroup[];
		/** Lists the mailboxes of each group (several mailboxes at stake). */
		list?: boolean;
	}

	const SHOWN = 5;

	let { groups, list = false }: Props = $props();
	let busy = $state<number | null>(null);

	const serverName = (id: number | null) => (id == null ? '' : (data.servers.find((s) => s.id === id)?.name ?? ''));

	function text(group: GapGroup): string {
		switch (group.gap) {
			case 'dav_off':
				return m.migrations_srcdav_dav_off({ server: serverName(group.serverId) });
			case 'no_server':
				return m.migrations_srcdav_no_server();
			case 'ambiguous':
				return m.migrations_srcdav_ambiguous();
			case 'same_mailbox':
				return m.migrations_srcdav_same_mailbox();
			default:
				return m.migrations_srcdav_kind();
		}
	}

	async function enable(id: number) {
		const server = data.servers.find((s) => s.id === id);
		if (!server) return;
		busy = id;
		try {
			await data.enableDav(server);
		} finally {
			busy = null;
		}
	}
</script>

{#each groups as group (`${group.gap}:${group.serverId}`)}
	<Notice>
		<p class="m-0" data-testid="srcdav-notice" data-gap={group.gap}>{text(group)}</p>
		{#if list && group.accounts.length}
			<p class="m-0 mt-1 font-mono text-xs" data-testid="srcdav-list">
				{group.accounts.slice(0, SHOWN).map((a) => prefs.email(a.email)).join(', ')}{#if group.accounts.length > SHOWN}<span class="font-sans">{' '}{m.migrations_import_more({ count: group.accounts.length - SHOWN })}</span>{/if}
			</p>
		{/if}
		{#if group.gap === 'dav_off' && group.serverId != null}
			{@const id = group.serverId}
			<div class="mt-2"><Button icon={CalendarCheck} loading={busy === id} onclick={() => enable(id)} data-testid="srcdav-enable">{m.migrations_srcdav_enable({ server: serverName(id) })}</Button></div>
		{/if}
	</Notice>
{/each}
