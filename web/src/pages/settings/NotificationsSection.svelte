<script lang="ts">
	import Settings from '@lucide/svelte/icons/settings';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { enabledChannels } from '../../lib/logic/notify.ts';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import NotificationsForm from './NotificationsForm.svelte';

	onMount(() => {
		if (!intake.settings) void intake.load();
	});

	let editing = $state(false);

	/** What the row says at a glance: off, or the channels in use. */
	const summary = $derived.by(() => {
		const n = intake.settings?.notifications;
		if (!n?.enabled) return m.settings_state_off();
		const names = { ntfy: 'ntfy', webhook: m.settings_notifications_webhook(), email: m.settings_notifications_email() };
		return enabledChannels(n).map((c) => names[c]).join(', ') || m.settings_state_on();
	});
</script>

<div data-testid="section-notifications-body">
	{#if intake.settings}
		<SettingRow as="div" label={m.settings_notifications_enabled()}>
			<span class="min-w-0 truncate text-ink-2" data-testid="notify-state">{summary}</span>
			<IconButton icon={Settings} label={m.settings_notifications_configure()} onclick={() => (editing = true)} data-testid="notify-open" />
		</SettingRow>
		{#if editing}
			<NotificationsForm stored={intake.settings.notifications} onclose={() => (editing = false)} />
		{/if}
	{:else if intake.error}
		<div class="p-4"><ErrorNotice title={m.settings_intake_load_failed()} message={intake.error} onretry={() => intake.load()} /></div>
	{:else}
		<div class="p-4"><Skeleton class="h-8" /></div>
	{/if}
</div>
