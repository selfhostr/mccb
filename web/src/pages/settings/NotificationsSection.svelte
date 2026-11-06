<script lang="ts">
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import NotificationsForm from './NotificationsForm.svelte';

	onMount(() => {
		if (!intake.settings) void intake.load();
	});
</script>

<section id="notifications" class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid="section-notifications">
	<h2 class="text-[0.9375rem]">{m.settings_notifications_title()}</h2>
	<p class="m-0 mt-1 mb-2 text-ink-2">{m.settings_notifications_sentence()}</p>
	{#if intake.settings}
		<NotificationsForm stored={intake.settings.notifications} />
	{:else if intake.error}
		<ErrorNotice title={m.settings_intake_load_failed()} message={intake.error} onretry={() => intake.load()} />
	{:else}
		<Skeleton class="h-40" />
	{/if}
</section>
