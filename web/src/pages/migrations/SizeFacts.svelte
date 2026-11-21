<script lang="ts">
	/** Size of the mail at the source and bytes imported from it, as two entries of the <dl> they are placed in. */
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { Account } from '../../lib/api/types.ts';
	import { fmtBytes } from '../../lib/format.ts';
	import { importedBytes } from '../../lib/logic/accounts.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import SizeTotal from './SizeTotal.svelte';

	let { id }: { id: number } = $props();

	const size = $derived(data.accounts.get(id)?.source_size);
	const imported = $derived(importedBytes(size, data.jobs.get(id)));
	const hasJob = $derived(data.jobs.has(id));

	async function measure() {
		try {
			data.upsertAccount(await post<Account>(`/accounts/${id}/size`));
		} catch (e) {
			toast(errorText(e), 'warning');
		}
	}
</script>

<div data-testid="size-source">
	<dt class="text-ink-3">{m.migrations_size_source()}</dt>
	<dd class="m-0 flex items-center gap-1">
		<span class="font-mono"><SizeTotal {size} /></span>
		{#if size?.measurable && !size.computing}
			<IconButton icon={RefreshCw} label={m.migrations_size_refresh()} onclick={measure} data-testid="size-refresh" />
		{/if}
	</dd>
	{#if size?.bytes != null && size.at}
		<dd class="m-0 text-ink-3">{m.migrations_size_measured()} <RelTime value={size.at} /></dd>
	{:else if size?.bytes == null && !size?.computing && !size?.measurable}
		<dd class="m-0 text-ink-3">{m.migrations_size_after_job()}</dd>
	{/if}
</div>
{#if hasJob || imported}
	<div data-testid="size-imported">
		<dt class="text-ink-3">{m.migrations_size_imported()}</dt>
		<dd class="m-0 font-mono">{fmtBytes(imported)}</dd>
	</div>
{/if}
