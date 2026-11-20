<script lang="ts">
	/**
	 * Changes the same things on every selected mailbox: its server, the domain of its address, a common
	 * password, what is copied. A field left on "keep" changes nothing. One request per mailbox; the ones
	 * that fail are listed and the others stay changed.
	 */
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, put } from '../../lib/api/client.ts';
	import type { Account, AccountOptions, Server } from '../../lib/api/types.ts';
	import { hasOwnTarget } from '../../lib/logic/connector.ts';
	import { sourceGaps } from '../../lib/logic/sourceDav.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import NoDavNotice from './NoDavNotice.svelte';
	import SourceGapNotice from './SourceGapNotice.svelte';

	let { ids, onclose }: { ids: number[]; onclose: () => void } = $props();

	const accounts = $derived(ids.map((id) => data.accounts.get(id)).filter((a): a is Account => !!a));
	const KEYS = ['mail', 'calendar', 'contacts'] as const;

	let serverId = $state('');
	let domain = $state('');
	let password = $state('');
	let content = $state<Record<(typeof KEYS)[number], string>>({ mail: '', calendar: '', contacts: '' });
	let mirror = $state('');
	let busy = $state(false);
	let failures = $state<string[]>([]);

	const label = (k: (typeof KEYS)[number]) => ({ mail: m.migrations_row_content_mail(), calendar: m.migrations_row_content_calendar(), contacts: m.migrations_row_content_contacts() })[k];
	const cleanDomain = $derived(domain.trim().replace(/^@/, ''));
	const swap = (value: string) => (value.includes('@') ? `${value.slice(0, value.lastIndexOf('@'))}@${cleanDomain}` : value);
	const changes = $derived(!!serverId || !!cleanDomain || !!password || !!mirror || KEYS.some((k) => content[k]));
	// Calendars or contacts turned on here: the mailboxes whose source cannot bring them, and the destination
	// servers with DAV off, each with its fix (one click on the server fixes all its mailboxes).
	const turnedOn = (k: 'calendar' | 'contacts') => content[k] === 'on';
	const serverOf = (id: number) => data.servers.find((s) => s.id === id);
	const srcGaps = $derived(sourceGaps(accounts, (_a, k) => turnedOn(k), serverOf));
	const noDav = $derived.by(() => {
		if (!turnedOn('calendar') && !turnedOn('contacts')) return [];
		const out = new Map<number, { server: Server; accounts: Account[] }>();
		for (const a of accounts) {
			const server = serverOf(serverId ? Number(serverId) : a.server_id);
			if (!server || server.dav_enabled || hasOwnTarget(a)) continue;
			const group = out.get(server.id) ?? { server, accounts: [] };
			group.accounts.push(a);
			out.set(server.id, group);
		}
		return [...out.values()];
	});
	const example = $derived(cleanDomain && accounts[0] ? `${prefs.email(accounts[0].email)} → ${prefs.email(swap(accounts[0].email))}` : '');

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !changes) return;
		busy = true;
		failures = [];
		let done = 0;
		for (const a of accounts) {
			const body: Record<string, unknown> = {};
			if (serverId) body.server_id = Number(serverId);
			if (cleanDomain) {
				body.email = swap(a.email);
				if (a.login && a.login.includes('@')) body.login = swap(a.login);
			}
			if (password) body.password = password;
			const options: Partial<AccountOptions> = {};
			for (const k of KEYS) if (content[k]) options[k] = content[k] === 'on';
			if (mirror) options.mirror = mirror === 'on';
			if (Object.keys(options).length) body.options = options;
			try {
				data.upsertAccount(await put<Account>(`/accounts/${a.id}`, body));
				done++;
			} catch (err) {
				failures.push(`${prefs.email(a.email)}: ${errorText(err)}`);
			}
		}
		busy = false;
		if (done) toast(m.migrations_bulk_done({ count: done }), 'signal');
		if (!failures.length) onclose();
	}
</script>

<Dialog title={m.migrations_bulk_title({ count: accounts.length })} {onclose}>
	<form id="bulk-form" class="flex flex-col gap-5" onsubmit={submit}>
		<p class="m-0 text-ink-2">{m.migrations_bulk_hint()}</p>
		<Field label={m.migrations_bulk_server()}>
			<select class="input" bind:value={serverId}>
				<option value="">{m.migrations_bulk_keep()}</option>
				{#each data.servers as s (s.id)}<option value={String(s.id)}>{s.name}</option>{/each}
			</select>
		</Field>
		<Field label={m.migrations_bulk_domain()} hint={example}>
			<input class="input font-mono" autocomplete="off" placeholder={m.migrations_bulk_keep()} bind:value={domain} />
		</Field>
		<Field label={m.migrations_bulk_password()}>
			<input class="input" type="password" autocomplete="new-password" placeholder={m.migrations_bulk_keep()} bind:value={password} />
		</Field>
		<div class="grid grid-cols-3 gap-2">
			{#each KEYS as k (k)}
				<Field label={label(k)}>
					<select class="input" bind:value={content[k]}>
						<option value="">{m.migrations_bulk_keep()}</option>
						<option value="on">{m.migrations_bulk_on()}</option>
						<option value="off">{m.migrations_bulk_off()}</option>
					</select>
				</Field>
			{/each}
		</div>
		{#if srcGaps.length || noDav.length}
			<div class="flex flex-col gap-2" data-testid="bulk-content-notices">
				<SourceGapNotice groups={srcGaps} list />
				{#each noDav as group (group.server.id)}<NoDavNotice server={group.server} accounts={group.accounts} />{/each}
			</div>
		{/if}
		<Field label={m.migrations_mirror_label()} hint={m.migrations_mirror_help()}>
			<select class="input" bind:value={mirror} data-testid="bulk-mirror-select">
				<option value="">{m.migrations_bulk_keep()}</option>
				<option value="on">{m.migrations_bulk_mirror_on()}</option>
				<option value="off">{m.migrations_bulk_mirror_off()}</option>
			</select>
		</Field>
		{#if failures.length}<ErrorNotice title={m.migrations_bulk_failed({ count: failures.length })} message={failures.join('\n')} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="bulk-form" loading={busy} disabled={!changes}>{m.migrations_bulk_apply({ count: accounts.length })}</Button>
	{/snippet}
</Dialog>
