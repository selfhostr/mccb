<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import Send from '@lucide/svelte/icons/send';
	import X from '@lucide/svelte/icons/x';
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { IntakeSettings, NotificationEvent, NotifyChannel, NotifyTestResult } from '../../lib/api/types.ts';
	import { EVENT_GROUPS, enabledChannels, isPublicNtfy, notificationsDirty, notificationsPatch } from '../../lib/logic/notify.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import SecretInput from '../../lib/ui/SecretInput.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';

	type Notifications = IntakeSettings['notifications'];
	let { stored, onclose }: { stored: Notifications; onclose: () => void } = $props();

	// The form starts from what is stored and then belongs to the person typing.
	let draft = $state<Notifications>(untrack(() => $state.snapshot(stored) as Notifications));
	let ntfyToken = $state({ value: '', clear: false });
	let webhookToken = $state({ value: '', clear: false });
	let emailPassword = $state({ value: '', clear: false });
	let busy = $state(false);
	let testing = $state(false);
	let error = $state<string | null>(null);
	let testError = $state<string | null>(null);
	let results = $state.raw<NotifyTestResult['results'] | null>(null);

	const secrets = $derived({ ntfy: ntfyToken, webhook: webhookToken, email: emailPassword });
	const dirty = $derived(notificationsDirty(draft, stored, secrets));
	const publicNtfy = $derived((draft.ntfy.enabled || draft.ntfy.topic !== '') && isPublicNtfy(draft.ntfy.url));
	const onCount = $derived(Object.values(draft.events).filter(Boolean).length);

	const EVENT_LABELS = $derived<Record<NotificationEvent, string>>({
		upload_finished: m.settings_notifications_ev_upload_finished(),
		analysis_valid: m.settings_notifications_ev_analysis_valid(),
		analysis_invalid: m.settings_notifications_ev_analysis_invalid(),
		job_done: m.settings_notifications_ev_job_done(),
		job_done_with_errors: m.settings_notifications_ev_job_done_with_errors(),
		job_failed: m.settings_notifications_ev_job_failed(),
		job_stopped: m.settings_notifications_ev_job_stopped(),
		phase_mail: m.settings_notifications_ev_phase_mail(),
		phase_calendar: m.settings_notifications_ev_phase_calendar(),
		phase_contacts: m.settings_notifications_ev_phase_contacts(),
		batch_finished: m.settings_notifications_ev_batch_finished(),
		disk_low: m.settings_notifications_ev_disk_low(),
		repeated_crash: m.settings_notifications_ev_repeated_crash()
	});
	const GROUP_LABELS = $derived({
		files: m.settings_notifications_group_files(),
		migrations: m.settings_notifications_group_migrations(),
		phases: m.settings_notifications_group_phases(),
		server: m.settings_notifications_group_server()
	});
	const CHANNEL_LABELS = $derived<Record<NotifyChannel, string>>({
		ntfy: 'ntfy',
		webhook: m.settings_notifications_webhook(),
		email: m.settings_notifications_email()
	});

	async function save(): Promise<boolean> {
		busy = true;
		error = null;
		try {
			await intake.save(notificationsPatch(draft, secrets));
			ntfyToken = { value: '', clear: false };
			webhookToken = { value: '', clear: false };
			emailPassword = { value: '', clear: false };
			return true;
		} catch (err) {
			error = errorText(err);
			return false;
		} finally {
			busy = false;
		}
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (await save()) toast(m.settings_notifications_saved(), 'signal');
	}

	/** A test uses what the server has stored, so unsaved edits are saved first. */
	async function test() {
		testError = null;
		results = null;
		if (dirty && !(await save())) return;
		testing = true;
		try {
			results = (await post<NotifyTestResult>('/intake/notifications/test', {})).results;
		} catch (err) {
			testError = errorText(err);
		} finally {
			testing = false;
		}
	}
</script>

<Dialog title={m.settings_notifications_title()} {onclose}>
	<form id="notifications-form" class="flex flex-col gap-4" onsubmit={submit} data-testid="notifications-form">
		<div class="divide-y divide-line rounded-card border border-line-strong">
			<SettingRow label={m.settings_notifications_enabled()}>
				<span class="check"><input class="switch" type="checkbox" checked={draft.enabled} onchange={(e) => (draft.enabled = e.currentTarget.checked)} /></span>
			</SettingRow>
			<SettingRow label={m.settings_notifications_mask()}>
				<span class="check"><input class="switch" type="checkbox" checked={draft.mask_addresses} onchange={(e) => (draft.mask_addresses = e.currentTarget.checked)} /></span>
			</SettingRow>
		</div>

		<div class="rounded-card border border-line-strong" data-testid="notify-ntfy">
			<SettingRow label={m.settings_notifications_ntfy_use()}>
				<span class="check"><input class="switch" type="checkbox" checked={draft.ntfy.enabled} onchange={(e) => (draft.ntfy.enabled = e.currentTarget.checked)} /></span>
			</SettingRow>
			{#if draft.ntfy.enabled}
				<div class="grid grid-cols-1 gap-3 border-t border-line p-4">
					<Field label={m.settings_notifications_url()}>
						<input class="input font-mono" type="url" autocomplete="off" bind:value={draft.ntfy.url} data-testid="ntfy-url" />
					</Field>
					<Field label={m.settings_notifications_topic()}>
						<input class="input font-mono" autocomplete="off" spellcheck="false" bind:value={draft.ntfy.topic} data-testid="ntfy-topic" />
					</Field>
					<SecretInput label={m.settings_notifications_token()} hasSecret={stored.ntfy.has_token} bind:value={ntfyToken.value} bind:clear={ntfyToken.clear} data-testid="ntfy-token" />
					<Field label={m.settings_notifications_priority()}>
						<select class="input" bind:value={draft.ntfy.priority} data-testid="ntfy-priority">
							{#each [1, 2, 3, 4, 5] as level (level)}<option value={level}>{level}</option>{/each}
						</select>
					</Field>
					{#if publicNtfy}
						<Notice><p class="m-0" data-testid="ntfy-public-warning">{m.settings_notifications_public_warning()}</p></Notice>
					{/if}
				</div>
			{/if}
		</div>

		<div class="rounded-card border border-line-strong" data-testid="notify-webhook">
			<SettingRow label={m.settings_notifications_webhook_use()}>
				<span class="check"><input class="switch" type="checkbox" checked={draft.webhook.enabled} onchange={(e) => (draft.webhook.enabled = e.currentTarget.checked)} /></span>
			</SettingRow>
			{#if draft.webhook.enabled}
				<div class="grid grid-cols-1 gap-3 border-t border-line p-4">
					<Field label={m.settings_notifications_url()}>
						<input class="input font-mono" type="url" autocomplete="off" bind:value={draft.webhook.url} data-testid="webhook-url" />
					</Field>
					<SecretInput label={m.settings_notifications_token()} hasSecret={stored.webhook.has_token} bind:value={webhookToken.value} bind:clear={webhookToken.clear} data-testid="webhook-token" />
				</div>
			{/if}
		</div>

		<div class="rounded-card border border-line-strong" data-testid="notify-email">
			<SettingRow label={m.settings_notifications_email_use()}>
				<span class="check"><input class="switch" type="checkbox" checked={draft.email.enabled} onchange={(e) => (draft.email.enabled = e.currentTarget.checked)} /></span>
			</SettingRow>
			{#if draft.email.enabled}
				<div class="grid grid-cols-1 gap-3 border-t border-line p-4">
					<Field label={m.settings_notifications_email_host()}><input class="input font-mono" autocomplete="off" bind:value={draft.email.host} data-testid="email-host" /></Field>
					<div class="grid grid-cols-2 gap-3">
						<Field label={m.settings_notifications_email_port()}><input class="input font-mono" type="number" min="1" max="65535" bind:value={draft.email.port} /></Field>
						<Field label={m.settings_notifications_email_security()}>
							<select class="input" bind:value={draft.email.security}>
								<option value="starttls">{m.settings_server_security_starttls()}</option>
								<option value="ssl">{m.settings_server_security_ssl()}</option>
								<option value="none">{m.settings_server_security_none()}</option>
							</select>
						</Field>
					</div>
					<Field label={m.settings_notifications_email_username()}><input class="input font-mono" autocomplete="off" bind:value={draft.email.username} /></Field>
					<SecretInput label={m.settings_notifications_email_password()} hasSecret={stored.email.has_password} bind:value={emailPassword.value} bind:clear={emailPassword.clear} />
					<Field label={m.settings_notifications_email_from()}><input class="input font-mono" type="email" autocomplete="off" bind:value={draft.email.from} data-testid="email-from" /></Field>
					<Field label={m.settings_notifications_email_to()}><input class="input font-mono" autocomplete="off" bind:value={draft.email.to} data-testid="email-to" /></Field>
				</div>
			{/if}
		</div>

		<div class="rounded-card border border-line-strong px-4 py-1">
			<Disclosure title={m.settings_notifications_events()} summary={m.settings_notifications_events_on({ count: onCount })} data-testid="notify-events">
				<div class="flex flex-col gap-2">
					{#each EVENT_GROUPS as group (group.id)}
						<fieldset class="m-0 min-w-0 border-0 p-0">
							<legend class="p-0 text-xs font-medium text-ink-3">{GROUP_LABELS[group.id]}</legend>
							<div class="flex flex-col">
								{#each group.events as event (event)}
									<Toggle checked={draft.events[event]} label={EVENT_LABELS[event]} onchange={(v) => (draft.events[event] = v)} />
								{/each}
							</div>
						</fieldset>
					{/each}
				</div>
			</Disclosure>
		</div>

		{#if error}<ErrorNotice title={m.settings_notifications_save_failed()} message={error} />{/if}
		{#if testError}<p class="m-0 text-warning-ink" role="alert" data-testid="notify-test-error">{testError}</p>{/if}
		{#if results}
			<ul class="m-0 flex list-none flex-col gap-1 p-0" data-testid="notify-results" aria-live="polite">
				{#each Object.entries(results) as [channel, result] (channel)}
					<li class="flex items-start gap-2" data-channel={channel}>
						<span class="w-28 shrink-0"><Plate tone={result.ok ? 'signal' : 'warning'} icon={result.ok ? Check : X} label={CHANNEL_LABELS[channel as NotifyChannel] ?? channel} /></span>
						<span class="min-w-0 text-xs break-words text-ink-2">{result.ok ? m.settings_notifications_test_ok() : prefs.text(result.error ?? m.settings_notifications_test_failed())}</span>
					</li>
				{/each}
			</ul>
		{/if}
	</form>

	{#snippet actions()}
		<Button icon={Send} loading={testing} disabled={!dirty && enabledChannels(stored).length === 0} onclick={test} data-testid="notify-test">{m.settings_notifications_test()}</Button>
		<Button variant="primary" type="submit" form="notifications-form" loading={busy} disabled={!dirty} data-testid="notify-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
