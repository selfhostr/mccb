<script lang="ts">
	import ArrowLeft from '@lucide/svelte/icons/arrow-left';
	import ExternalLink from '@lucide/svelte/icons/external-link';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post, put } from '../../lib/api/client.ts';
	import type { Server, ServerInput } from '../../lib/api/types.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import PolicyFields from './PolicyFields.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { davDetected } from '../../lib/logic/dav.ts';
	import { providerOf, type Provider } from '../../lib/logic/providers.ts';

	interface Props {
		server?: Server | null;
		/** Adding: the provider picked on the way in. Its preset fills host, port, security and the DAV addresses. */
		provider?: Provider;
		onclose: () => void;
		onback?: () => void;
		onsaved?: (saved: Server) => void;
	}
	let { server = null, provider: picked, onclose, onback, onsaved }: Props = $props();

	const initial: ServerInput = untrack(() =>
		server
			? { ...server }
			: { name: '', imap_host: '', imap_port: 993, imap_security: 'ssl', verify_tls: true, root_folder: '', caldav_url: '', carddav_url: '', dav_enabled: false, workers: 4, notes: '', tuning: 'auto', builders: null, ...(picked?.preset ?? {}) }
	);
	let form = $state<ServerInput>(initial);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let policyError = $state<string | null>(null);
	let policy = $state<PolicyFields>();
	const provider = untrack(() => picked ?? providerOf(server?.imap_host));
	const LINKS = {
		gmail_password: () => m.servers_link_gmail_password(),
		gmail_imap: () => m.servers_link_gmail_imap(),
		icloud_password: () => m.servers_link_icloud_password()
	};
	const note = $derived(
		provider.id === 'gmail' ? m.servers_note_gmail() : provider.id === 'icloud' ? m.servers_note_icloud() : provider.note === 'oauth' ? m.settings_server_provider_oauth() : provider.note ? m.settings_server_provider_app_password({ name: provider.name }) : null
	);

	function onSecurity() {
		if (form.imap_port === 993 || form.imap_port === 143) form.imap_port = form.imap_security === 'ssl' ? 993 : 143;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = null;
		const { id: _id, ...rest } = { id: 0, ...$state.snapshot(form) };
		const body = { ...rest, root_folder: rest.root_folder || null, caldav_url: rest.caldav_url || null, carddav_url: rest.carddav_url || null };
		policyError = null;
		try {
			// The rules first for an existing server: a refusal leaves everything as it was.
			if (server && policy?.dirty()) {
				try {
					await put(`/servers/${server.id}/policy`, policy.body());
				} catch (err) {
					policyError = errorText(err);
					return;
				}
			}
			const saved = server ? await put<Server>(`/servers/${server.id}`, body) : await post<Server>('/servers', body);
			await data.reloadServers();
			toast(m.settings_server_saved({ name: body.name }), 'signal');
			onclose();
			// Checked right away: the list shows at a glance whether the server answers.
			onsaved?.(saved);
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={server ? m.settings_server_edit_title({ name: server.name }) : m.servers_kind_title({ name: provider.id === 'imap' ? m.servers_type_imap() : provider.name })} {onclose} wide>
	<form id="server-form" class="flex flex-col gap-3" onsubmit={submit}>
		{#if !server && note}
			<div class="rounded-btn bg-surface-2 p-3 text-sm text-ink-2" data-testid="server-note">
				<p class="m-0">{note}</p>
				{#if provider.links?.length}
					<p class="m-0 mt-1 flex flex-wrap gap-x-4 gap-y-1">
						{#each provider.links as link (link.id)}
							<a class="inline-flex min-h-10 items-center gap-1 font-medium" href={link.href} target="_blank" rel="noopener noreferrer" data-link={link.id}>{LINKS[link.id]()}<ExternalLink size={14} /></a>
						{/each}
					</p>
				{/if}
			</div>
		{/if}
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
		{#if server && !form.dav_enabled && davDetected(server, server.last_test_result)}
			<Notice><p class="m-0" data-testid="server-dialog-dav-found">{m.settings_server_dav_found()}</p></Notice>
		{/if}
		{#if form.dav_enabled}
			<Field label={m.settings_server_caldav()} hint={m.settings_server_dav_hint({ email: "{email}", user: "{user}", domain: "{domain}" })}>
				<input class="input font-mono" bind:value={form.caldav_url} />
			</Field>
			<Field label={m.settings_server_carddav()}>
				<input class="input font-mono" bind:value={form.carddav_url} />
			</Field>
		{/if}
		<Disclosure title={m.settings_server_tuning()}>
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
		</Disclosure>
		{#if server}<PolicyFields bind:this={policy} url={`/servers/${server.id}/policy`} server />{/if}
		{#if policyError}<ErrorNotice title={m.policy_save_failed()} message={policyError} />{/if}
		{#if error}<ErrorNotice title={m.settings_server_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		{#if onback}<span class="mr-auto"><Button variant="ghost" icon={ArrowLeft} onclick={onback}>{m.common_back()}</Button></span>{/if}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="server-form" loading={busy} data-testid="server-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
