<script lang="ts">
	/** Many mailboxes at once: email;password;login. Each one is tested right away. */
	import Download from '@lucide/svelte/icons/download';
	import Upload from '@lucide/svelte/icons/upload';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { repairs } from '../../lib/stores/repair.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	let { onclose }: { onclose: () => void } = $props();

	interface Result {
		created: number;
		updated: number;
		errors: { line?: number; msg: string }[];
		ids: number[];
	}

	let serverId = $state(untrack(() => data.servers[0]?.id ?? 0));
	let input = $state<HTMLInputElement>();
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<Result | null>(null);

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
			result = await post<Result>('/repair/boxes/import-csv', body);
			await repairs.refresh();
			if (result.ids.length) void repairs.test(result.ids);
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.repair_csv_title()} {onclose}>
	<form id="repair-csv-form" class="flex flex-col gap-3" onsubmit={submit}>
		<p class="m-0 text-ink-2">{m.migrations_csv_columns()}</p>
		<code class="rounded-plate bg-surface-2 p-2 font-mono text-xs">email;password;login</code>
		<Field label={m.repair_box_server()}>
			<select class="input" bind:value={serverId}>
				{#each data.servers as server (server.id)}
					<option value={server.id}>{server.name}</option>
				{/each}
			</select>
		</Field>
		<Field label={m.migrations_csv_file()}>
			<input bind:this={input} class="input py-2" type="file" accept=".csv,.txt" required data-testid="repair-csv-file" />
		</Field>
		<div><Button variant="ghost" icon={Download} href="/api/accounts/csv-template" download="accounts.csv">{m.migrations_csv_template()}</Button></div>
		{#if result}
			<p class="m-0" data-testid="repair-csv-result">{m.migrations_csv_result({ created: result.created, updated: result.updated, errors: result.errors.length })}</p>
			{#if result.errors.length}
				<Notice>
					<ul class="m-0 list-none p-0 font-mono text-xs">
						{#each result.errors as err, i (i)}
							<li>{err.line ? `${err.line}: ` : ''}{err.msg}</li>
						{/each}
					</ul>
				</Notice>
			{/if}
		{/if}
		{#if error}<ErrorNotice title={m.repair_csv_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{result ? m.common_close() : m.common_cancel()}</Button>
		<Button variant="primary" icon={Upload} type="submit" form="repair-csv-form" loading={busy} data-testid="repair-csv-submit">{m.repair_csv_submit()}</Button>
	{/snippet}
</Dialog>
