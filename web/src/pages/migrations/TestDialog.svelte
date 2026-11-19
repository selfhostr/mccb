<script lang="ts">
	/**
	 * The destination's last test, as the server stored it, one line per protocol. When the source is a connector
	 * it is tried too, and said apart: the stored test only ever looks at the destination.
	 */
	import Check from '@lucide/svelte/icons/check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Minus from '@lucide/svelte/icons/minus';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { Account } from '../../lib/api/types.ts';
	import { currentResult } from '../../lib/logic/accounts.ts';
	import { endpointSummary } from '../../lib/logic/connector.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import { sourceProbes, sourceTestable, testSource } from './ConnectionCell.svelte';

	let { accountId, onclose }: { accountId: number; onclose: () => void } = $props();

	const account = $derived(data.accounts.get(accountId) as Account | undefined);
	const result = $derived(account ? currentResult(account) : null);
	const phase = $derived(data.testing.get(accountId));
	const testable = $derived(!!account && sourceTestable(account));
	const source = $derived(testable ? sourceProbes.get(accountId) : undefined);
	const sourceLabel = $derived(account ? (endpointSummary(connectors.list, account.source, 'source')?.label ?? '') : '');
	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;

	function retest() {
		data.test([accountId]);
		if (account && testable) void testSource(account);
	}
</script>

<Dialog title={m.migrations_details_title({ email: prefs.email(account?.email) })} {onclose} wide>
	<div class="flex flex-col gap-3">
		{#if account?.last_test}
			<p class="m-0 text-ink-2">{m.migrations_details_tested_prefix()} <RelTime value={account.last_test} /></p>
		{/if}
		{#if testable}<h3 class="m-0 text-sm font-semibold">{m.migrations_test_destination()}</h3>{/if}
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
		{#if testable}
			<section class="flex flex-col gap-2" data-testid="test-source">
				<h3 class="m-0 text-sm font-semibold">{m.migrations_row_source({ label: sourceLabel })}</h3>
				<div class="flex items-start gap-2">
					<span class="w-28 shrink-0">
						{#if !source}
							<Plate icon={CircleDashed} label={m.migrations_test_never()} />
						{:else if source.state === 'testing'}
							<Plate tone="info" icon={FlaskConical} label={m.migrations_test_source_running()} />
						{:else}
							<Plate tone={source.ok ? 'signal' : 'warning'} icon={source.ok ? Check : X} label={source.ok ? m.migrations_test_source_ok() : m.migrations_test_source_failed()} />
						{/if}
					</span>
					{#if source?.state === 'done'}
						<span class="min-w-0 flex-1 font-mono text-xs break-words">{prefs.text(source.msg) || m.migrations_details_no_message()}</span>
						{#if source.msg}<CopyButton text={prefs.text(source.msg)} />{/if}
					{/if}
				</div>
			</section>
		{/if}
	</div>
	{#snippet actions()}
		<Button variant="primary" icon={FlaskConical} loading={!!phase || source?.state === 'testing'} onclick={retest}>{m.migrations_details_retest()}</Button>
	{/snippet}
</Dialog>
