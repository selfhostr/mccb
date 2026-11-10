<script lang="ts">
	/**
	 * The destination of one tenant mailbox, chosen by hand: a box the server already has, or a new one typed here
	 * (address and password, as a CSV line would give). The choice is a link of that box to the mailbox: it lasts
	 * across refreshes, and the box another mailbox receives cannot be chosen.
	 */
	import Check from '@lucide/svelte/icons/check';
	import Search from '@lucide/svelte/icons/search';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, post } from '../../lib/api/client.ts';
	import type { Account, MatchRow } from '../../lib/api/types.ts';
	import { choiceFor, pickChoices, searchChoices, type PickChoice } from '../../lib/logic/tenantmatch.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';

	interface Props {
		row: MatchRow;
		rows: readonly MatchRow[];
		tenantId: number;
		serverId: number;
		server: string;
		ondone: (accountId: number, email: string) => unknown;
		onclose: () => void;
	}

	let { row, rows, tenantId, serverId, server, ondone, onclose }: Props = $props();

	const NEW = 'new';
	const mailbox = $derived(row.mailbox!);
	const choices = $derived(pickChoices(rows, mailbox.id));
	let q = $state('');
	const shown = $derived(searchChoices(choices, q));
	let picked = $state<string>('');
	let email = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	// A box created here whose link then failed: a second try uses it instead of creating it again.
	let created: { id: number; email: string } | null = null;

	function why(c: PickChoice): string {
		if (c.block === 'current') return m.tenant_pick_current();
		if (c.block === 'taken') return m.tenant_pick_taken({ address: prefs.email(c.takenBy ?? '') });
		if (c.block === 'other_source') return m.tenant_pick_other_source();
		return '';
	}

	function chooseNew() {
		picked = NEW;
		if (!email && q.includes('@')) email = q.trim();
	}

	/** The first refusal of a link, in the server's words. */
	function refusal(err: unknown): string {
		if (err instanceof ApiError && err.body && typeof err.body === 'object') {
			const detail = (err.body as { detail?: { errors?: { msg?: string }[] } }).detail;
			const msg = detail && typeof detail === 'object' ? detail.errors?.[0]?.msg : undefined;
			if (msg) return msg;
		}
		return errorText(err);
	}

	async function run(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !picked) return;
		error = null;
		let accountId: number;
		let address: string;
		let fresh = false;
		if (picked === NEW) {
			const typed = email.trim();
			const known = choiceFor(choices, typed);
			if (known && known.block) {
				error = m.tenant_pick_exists_blocked({ email: prefs.email(known.account.email), reason: why(known) });
				return;
			}
			if (known) {
				accountId = known.account.id;
				address = known.account.email;
			} else if (created && created.email.toLowerCase() === typed.toLowerCase()) {
				accountId = created.id;
				address = created.email;
			} else {
				accountId = 0;
				address = typed;
				fresh = true;
			}
		} else {
			const c = choices.find((x) => String(x.account.id) === picked);
			if (!c || c.block) return;
			accountId = c.account.id;
			address = c.account.email;
		}
		busy = true;
		try {
			if (fresh) {
				const saved = await post<Account>('/accounts', { server_id: serverId, email: address, password });
				data.upsertAccount(saved);
				created = { id: saved.id, email: saved.email };
				accountId = saved.id;
				address = saved.email;
			}
			try {
				await sources.link(tenantId, { server_id: serverId, links: [{ account_id: accountId, mailbox_id: mailbox.id, manual: true }] });
			} catch (err) {
				throw new Error(refusal(err));
			}
			if (fresh) data.test([accountId]);
			await ondone(accountId, address);
			onclose();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.tenant_pick_title()} {onclose} wide>
	<form id="tenant-pick" class="flex flex-col gap-3" onsubmit={run} data-testid="tenant-pick-dialog">
		<p class="m-0">{m.tenant_pick_text({ mailbox: prefs.email(mailbox.mail || mailbox.upn), server })}</p>

		<label class="relative">
			<span class="sr-only">{m.tenant_pick_search()}</span>
			<span class="pointer-events-none absolute top-3 left-3 text-ink-3"><Search size={14} /></span>
			<input class="input pl-8" type="search" placeholder={m.tenant_pick_search()} bind:value={q} data-testid="tenant-pick-q" />
		</label>

		<div class="flex max-h-[min(18rem,40dvh)] flex-col divide-y divide-line overflow-y-auto rounded-btn border border-line" role="radiogroup" aria-label={m.tenant_pick_list({ server })} data-testid="tenant-pick-list">
			{#each shown as c (c.account.id)}
				{@const blocked = c.block !== null}
				<label class={`flex min-h-11 items-center gap-3 px-3 py-1.5 ${blocked ? 'text-ink-3' : 'cursor-pointer hover:bg-surface-2'}`} data-testid="tenant-pick-option" data-email={c.account.email} data-blocked={c.block ?? ''}>
					<input type="radio" name="tenant-pick" value={String(c.account.id)} disabled={blocked} bind:group={picked} />
					<span class="min-w-0 flex-1">
						<span class="block truncate font-mono text-sm">{prefs.email(c.account.email)}</span>
						{#if blocked}<span class="block truncate text-xs">{why(c)}</span>{/if}
					</span>
					{#if c.block === 'current'}<Check size={14} class="shrink-0 text-signal-ink" />{/if}
				</label>
			{:else}
				<p class="m-0 px-3 py-2 text-sm text-ink-3">{m.tenant_pick_none()}</p>
			{/each}
			<label class="flex min-h-11 cursor-pointer items-center gap-3 px-3 py-1.5 hover:bg-surface-2" data-testid="tenant-pick-new">
				<input type="radio" name="tenant-pick" value={NEW} bind:group={picked} onchange={chooseNew} />
				<span class="min-w-0 flex-1 text-sm font-medium">{m.tenant_pick_new()}</span>
			</label>
		</div>

		{#if picked === NEW}
			<div class="grid gap-3 sm:grid-cols-2">
				<Field label={m.tenant_pick_email()} hint={m.tenant_pick_new_hint({ server })}>
					<input class="input" type="email" required autocomplete="off" bind:value={email} data-testid="tenant-pick-email" />
				</Field>
				<Field label={m.tenant_pick_password()}>
					<input class="input" type="password" required autocomplete="new-password" bind:value={password} data-testid="tenant-pick-password" />
				</Field>
			</div>
		{/if}

		{#if error}<ErrorNotice title={m.tenant_pick_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="tenant-pick" icon={Check} loading={busy} disabled={!picked} data-testid="tenant-pick-confirm">{m.tenant_pick_confirm()}</Button>
	{/snippet}
</Dialog>
