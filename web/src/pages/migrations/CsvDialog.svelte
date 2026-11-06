<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import Upload from '@lucide/svelte/icons/upload';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { CsvResult } from '../../lib/api/types.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	let { defaultServer = null, onclose }: { defaultServer?: number | null; onclose: () => void } = $props();

	const initialServer = untrack(() => defaultServer ?? data.servers[0]?.id ?? 0);
	let serverId = $state(initialServer);
	let input = $state<HTMLInputElement>();
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<CsvResult | null>(null);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		const file = input?.files?.[0];
		if (!file) return;
		busy = true;
		error = null;
		const body = new FormData();
		body.append('file', file);
		body.append('server_id', String(serverId));
		try {
			result = await post<CsvResult>('/accounts/import-csv', body);
			await data.load();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.migrations_csv_title()} {onclose}>
	<form id="csv-form" class="flex flex-col gap-3" onsubmit={submit}>
		<p class="m-0 text-ink-2">{m.migrations_csv_columns()}</p>
		<code class="rounded-plate bg-surface-2 p-2 font-mono text-xs">email;password;login</code>
		<Field label={m.migrations_account_server()}>
			<select class="input" bind:value={serverId}>
				{#each data.servers as server (server.id)}
					<option value={server.id}>{server.name}</option>
				{/each}
			</select>
		</Field>
		<Field label={m.migrations_csv_file()}>
			<input bind:this={input} class="input py-2" type="file" accept=".csv,.txt" required />
		</Field>
		<Button variant="ghost" icon={Download} href="/api/accounts/csv-template" download="accounts.csv">{m.migrations_csv_template()}</Button>
		{#if error}<ErrorNotice title={m.migrations_csv_failed()} message={error} />{/if}
		{#if result}
			<p class="m-0" data-testid="csv-result">{m.migrations_csv_result({ created: result.created, updated: result.updated, errors: result.errors.length })}</p>
			{#if result.errors.length}
				<Notice>
					<ul class="m-0 list-none p-0 font-mono text-xs">
						{#each result.errors.slice(0, 50) as line (line.line)}
							<li>{m.migrations_csv_line({ line: line.line, message: line.msg })}</li>
						{/each}
					</ul>
				</Notice>
			{/if}
		{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_close()}</Button>
		<Button variant="primary" icon={Upload} type="submit" form="csv-form" loading={busy}>{m.migrations_csv_import()}</Button>
	{/snippet}
</Dialog>
