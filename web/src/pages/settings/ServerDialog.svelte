<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { Server, ServerInput } from '../../lib/api/types.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import Toggle from '../../lib/ui/Toggle.svelte';

	let { server = null, onclose }: { server?: Server | null; onclose: () => void } = $props();

	const initial: ServerInput = server
		? { ...server }
		: { name: '', imap_host: '', imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: '', caldav_url: '', carddav_url: '', dav_enabled: false, workers: 4, notes: '', tuning: 'auto', builders: null };
	let form = $state<ServerInput>(initial);
	let busy = $state(false);
	let error = $state<string | null>(null);

	function onSecurity() {
		if (form.imap_port === 993 || form.imap_port === 143) form.imap_port = form.imap_security === 'ssl' ? 993 : 143;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		const { id: _id, ...rest } = { id: 0, ...$state.snapshot(form) };
		const body = { ...rest, root_folder: rest.root_folder || null, caldav_url: rest.caldav_url || null, carddav_url: rest.carddav_url || null };
		try {
			if (server) await put(`/servers/${server.id}`, body);
			else await post('/servers', body);
			await data.reloadServers();
			toast(m.settings_server_saved({ name: body.name }), 'signal');
			onclose();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={server ? m.settings_server_edit_title({ name: server.name }) : m.settings_server_add_title()} {onclose} wide>
	<form id="server-form" class="flex flex-col gap-3" onsubmit={submit}>
		<Field label={m.settings_server_name()}>
			<input class="input" required bind:value={form.name} data-testid="server-name" />
		</Field>
		<div class="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_100px_minmax(0,1fr)]">
			<Field label={m.settings_server_host()}>
				<input class="input font-mono" required bind:value={form.imap_host} data-testid="server-host" />
			</Field>
			<Field label={m.settings_server_port()}>
				<input class="input font-mono" type="number" min="1" max="65535" required bind:value={form.imap_port} />
			</Field>
			<Field label={m.settings_server_security()}>
				<select class="input" bind:value={form.imap_security} onchange={onSecurity}>
					<option value="ssl">{m.settings_server_security_ssl()}</option>
					<option value="starttls">{m.settings_server_security_starttls()}</option>
					<option value="none">{m.settings_server_security_none()}</option>
				</select>
			</Field>
		</div>
		<Toggle checked={form.verify_tls} label={m.settings_server_verify_tls()} onchange={(v) => (form.verify_tls = v)} />
		<Field label={m.settings_server_root()} hint={m.settings_server_root_hint()}>
			<input class="input font-mono" bind:value={form.root_folder} />
		</Field>
		<Toggle checked={form.dav_enabled} label={m.settings_server_dav()} onchange={(v) => (form.dav_enabled = v)} />
		{#if form.dav_enabled}
			<Field label={m.settings_server_caldav()} hint={m.settings_server_dav_hint({ email: "{email}", user: "{user}", domain: "{domain}" })}>
				<input class="input font-mono" bind:value={form.caldav_url} />
			</Field>
			<Field label={m.settings_server_carddav()}>
				<input class="input font-mono" bind:value={form.carddav_url} />
			</Field>
		{/if}
		<details>
			<summary class="flex min-h-10 cursor-pointer items-center font-medium text-ink-2">{m.settings_server_tuning()}</summary>
			<div class="flex flex-col gap-3 pt-1">
				<Field label={m.settings_server_tuning_mode()}>
					<select class="input" bind:value={form.tuning}>
						<option value="auto">{m.settings_server_tuning_auto()}</option>
						<option value="manual">{m.settings_server_tuning_manual()}</option>
					</select>
				</Field>
				{#if form.tuning === 'manual'}
					<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
						<Field label={m.settings_server_workers()}>
							<input class="input font-mono" type="number" min="1" max="32" required bind:value={form.workers} />
						</Field>
						<Field label={m.settings_server_builders()} hint={m.settings_server_builders_hint()}>
							<input class="input font-mono" type="number" min="0" max="32" bind:value={form.builders} />
						</Field>
					</div>
				{/if}
				<Field label={m.settings_server_notes()}>
					<textarea class="input" rows="2" bind:value={form.notes}></textarea>
				</Field>
			</div>
		</details>
		{#if error}<ErrorNotice title={m.settings_server_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="server-form" loading={busy} data-testid="server-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
