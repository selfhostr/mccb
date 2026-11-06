<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { data } from '../../lib/stores/data.svelte.ts';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import AppearanceSection from './AppearanceSection.svelte';
	import IntakeSection from './IntakeSection.svelte';
	import NotificationsSection from './NotificationsSection.svelte';
	import PasswordSection from './PasswordSection.svelte';
	import ServersSection from './ServersSection.svelte';
	import TuningSection from './TuningSection.svelte';
</script>

<PageHeader title={m.settings_page_title()} sentence={m.settings_page_sentence({ servers: data.servers.length })} />

<div class="flex max-w-[860px] flex-col gap-8">
	<ServersSection />
	<IntakeSection />
	<NotificationsSection />
	<TuningSection />
	<PasswordSection />
	<AppearanceSection />
	{#if data.version}<p class="m-0 font-mono text-xs text-ink-3">{m.settings_page_version({ version: data.version })}</p>{/if}
</div>
