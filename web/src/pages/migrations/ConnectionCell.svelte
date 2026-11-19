<script module lang="ts">
	import { SvelteMap } from 'svelte/reactivity';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { Account, ConnectorProbe } from '../../lib/api/types.ts';
	import { hasCustomSource } from '../../lib/logic/connector.ts';

	/** A source test: the server keeps only the destination's, so this one lives for the session. */
	export type SourceProbe = { state: 'testing' } | { state: 'done'; ok: boolean; msg: string };

	export const sourceProbes = new SvelteMap<number, SourceProbe>();

	/** A connector source with its own settings can be tried as stored. A source read through a tenant cannot. */
	export const sourceTestable = (account: Pick<Account, 'source'>): boolean => hasCustomSource(account) && !account.source?.connection;

	/** Tries the stored source of a mailbox, with its stored secrets (the server reads them from the account). */
	export async function testSource(account: Account): Promise<void> {
		const kind = account.source?.kind;
		if (!kind || !sourceTestable(account)) return;
		sourceProbes.set(account.id, { state: 'testing' });
		try {
			const probe = await post<ConnectorProbe>(`/connectors/${encodeURIComponent(kind)}/test`, { role: 'source', config: account.source?.config ?? {}, account_id: account.id });
			sourceProbes.set(account.id, { state: 'done', ok: probe.ok, msg: probe.msg });
		} catch (error) {
			sourceProbes.set(account.id, { state: 'done', ok: false, msg: errorText(error) });
		}
	}
</script>

<script lang="ts">
	/** The last test as the server stored it: one word per protocol, never just IMAP. The source's state follows when it was tried. */
	import Check from '@lucide/svelte/icons/check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Minus from '@lucide/svelte/icons/minus';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { AccountStatus, TestResult } from '../../lib/api/types.ts';
	import type { TestPhase } from '../../lib/logic/testrun.ts';
	import { testLook } from '../../lib/status.ts';
	import Plate from '../../lib/ui/Plate.svelte';

	interface Props {
		email: string;
		phase: TestPhase | undefined;
		result: TestResult | null;
		status: AccountStatus;
		/** The source's last test in this session, when the source is a connector. */
		source?: SourceProbe;
		onclick: () => void;
	}

	let { email, phase, result, status, source, onclick }: Props = $props();

	const look = $derived(phase ? testLook(phase) : null);

	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;
</script>

<span class="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
	<span class="flex min-w-0 flex-wrap items-center gap-x-2">
		<span class="text-xs text-ink-3">{m.migrations_test_destination()}</span>
		{#if look}
			<span data-testid="test-state">
				<Plate tone={look.tone} icon={look.icon} label={look.label} busy={look.busy} />
			</span>
		{:else if result}
			<button type="button" class="flex min-h-10 flex-wrap items-center gap-x-2 rounded-plate text-left" aria-label={m.migrations_row_test_details({ email })} data-testid="test-result" {onclick}>
				{#each PROTOCOLS as protocol (protocol.key)}
					{@const probe = result[protocol.key]}
					{@const Icon = !probe ? Minus : probe.ok ? Check : X}
					<span class={`inline-flex items-center gap-0.5 text-xs font-medium ${!probe ? 'text-ink-3' : probe.ok ? 'text-signal-ink' : 'text-warning-ink'}`}>
						<Icon size={14} />{protocol.name}
					</span>
				{/each}
			</button>
		{:else if status !== 'new'}
			<!-- Tested before results were stored: only the status exists. -->
			<button type="button" class="flex min-h-10 items-center" aria-label={m.migrations_row_test_details({ email })} {onclick}>
				<Plate tone={status === 'ok' ? 'signal' : 'warning'} icon={status === 'ok' ? Check : X} label={status === 'ok' ? m.migrations_test_status_ok() : m.migrations_test_status_failed()} />
			</button>
		{:else}
			<Plate icon={CircleDashed} label={m.migrations_test_never()} />
		{/if}
	</span>
	{#if source}
		<span data-testid="source-test" title={source.state === 'done' ? source.msg : undefined}>
			{#if source.state === 'testing'}
				<Plate tone="info" icon={FlaskConical} label={m.migrations_test_source_running()} />
			{:else}
				<Plate tone={source.ok ? 'signal' : 'warning'} icon={source.ok ? Check : X} label={source.ok ? m.migrations_test_source_ok() : m.migrations_test_source_failed()} />
			{/if}
		</span>
	{/if}
</span>
