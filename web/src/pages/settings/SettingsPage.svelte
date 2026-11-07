<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { data } from '../../lib/stores/data.svelte.ts';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import AppearanceSection from './AppearanceSection.svelte';
	import IntakeSection from './IntakeSection.svelte';
	import NotificationsSection from './NotificationsSection.svelte';
	import PasswordSection from './PasswordSection.svelte';
	import TokensSection from './TokensSection.svelte';
	import TuningSection from './TuningSection.svelte';
</script>

<PageHeader title={m.settings_page_title()} sentence={m.settings_page_sentence({ servers: data.servers.length })} />

<div class="flex max-w-[960px] flex-col gap-10">
	<IntakeSection />
	<NotificationsSection />
	<TuningSection />
	<PasswordSection />
	<TokensSection />
	<AppearanceSection />
	{#if data.version}<p class="m-0 font-mono text-xs text-ink-3">{m.settings_page_version({ version: data.version })}</p>{/if}
</div>
