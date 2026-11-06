<script lang="ts">
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import NotificationsForm from './NotificationsForm.svelte';

	onMount(() => {
		if (!intake.settings) void intake.load();
	});
</script>

<Section id="notifications" title={m.settings_notifications_title()} sentence={m.settings_notifications_sentence()}>
	{#if intake.settings}
		<NotificationsForm stored={intake.settings.notifications} />
	{:else if intake.error}
		<ErrorNotice title={m.settings_intake_load_failed()} message={intake.error} onretry={() => intake.load()} />
	{:else}
		<Skeleton class="h-40" />
	{/if}
</Section>
