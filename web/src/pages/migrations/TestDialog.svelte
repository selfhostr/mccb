<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Minus from '@lucide/svelte/icons/minus';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account } from '../../lib/api/types.ts';
	import { currentResult } from '../../lib/logic/accounts.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';

	let { accountId, onclose }: { accountId: number; onclose: () => void } = $props();

	const account = $derived(data.accounts.get(accountId) as Account | undefined);
	const result = $derived(account ? currentResult(account) : null);
	const phase = $derived(data.testing.get(accountId));
	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;
</script>

<Dialog title={m.migrations_details_title({ email: prefs.email(account?.email) })} {onclose} wide>
	<div class="flex flex-col gap-3">
		{#if account?.last_test}
			<p class="m-0 text-ink-2">{m.migrations_details_tested_prefix()} <RelTime value={account.last_test} /></p>
		{/if}
		{#if result}
			<ul class="m-0 flex list-none flex-col gap-2 p-0">
				{#each PROTOCOLS as protocol (protocol.key)}
					{@const probe = result[protocol.key]}
					<li class="flex items-start gap-2 border-b border-line pb-2">
						<span class="w-28 shrink-0">
							<Plate tone={!probe ? 'ghost' : probe.ok ? 'signal' : 'warning'} icon={!probe ? Minus : probe.ok ? Check : X} label={protocol.name} />
						</span>
						<span class="min-w-0 flex-1 font-mono text-xs break-words">{prefs.text(probe?.msg) || m.migrations_details_no_message()}</span>
						{#if probe?.msg}<CopyButton text={prefs.text(probe.msg)} />{/if}
					</li>
				{/each}
			</ul>
		{:else}
			<p class="m-0 text-ink-2">{m.migrations_details_only_status()}</p>
		{/if}
	</div>
	{#snippet actions()}
		<Button variant="primary" icon={FlaskConical} loading={!!phase} onclick={() => data.test([accountId])}>{m.migrations_details_retest()}</Button>
	{/snippet}
</Dialog>
