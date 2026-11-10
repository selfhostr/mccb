<script lang="ts">
	/** The last test as the server stored it: one word per protocol, never just IMAP. */
	import Check from '@lucide/svelte/icons/check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
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
		onclick: () => void;
	}

	let { email, phase, result, status, onclick }: Props = $props();

	const look = $derived(phase ? testLook(phase) : null);

	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;
</script>

{#if look}
	<span data-testid="test-state">
		<Plate tone={look.tone} icon={look.icon} label={look.label} />
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
