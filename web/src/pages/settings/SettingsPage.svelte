<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { data } from '../../lib/stores/data.svelte.ts';
	import PageHeader from '../../lib/ui/PageHeader.svelte';
	import SettingsGroup from '../../lib/ui/SettingsGroup.svelte';
	import AppearanceSection from './AppearanceSection.svelte';
	import IntakeSection from './IntakeSection.svelte';
	import NotificationsSection from './NotificationsSection.svelte';
	import PasswordSection from './PasswordSection.svelte';
	import TokensSection from './TokensSection.svelte';
	import TuningSection from './TuningSection.svelte';

	const DOCS_URL = 'https://mccb.open.yety.ch/docs/';
	const RELEASES_URL = 'https://github.com/selfhostr/mccb/releases';
</script>

<PageHeader title={m.settings_page_title()} />

<div class="flex max-w-[720px] flex-col gap-6 pb-6">
	<SettingsGroup id="intake" title={m.settings_intake_title()}>
		<IntakeSection />
	</SettingsGroup>

	<SettingsGroup id="notifications" title={m.settings_notifications_title()}>
		<NotificationsSection />
	</SettingsGroup>

	<SettingsGroup id="security" title={m.settings_group_security_title()}>
		<PasswordSection />
		<TokensSection />
	</SettingsGroup>

	<SettingsGroup id="interface" title={m.settings_group_interface_title()}>
		<AppearanceSection />
		<TuningSection />
	</SettingsGroup>

	<footer class="flex flex-wrap items-center gap-x-4 text-xs text-ink-3" data-testid="section-about">
		{#if data.version}<span>{m.settings_about_version()} <span class="font-mono" data-testid="app-version">{data.version}</span></span>{/if}
		<a href={DOCS_URL} target="_blank" rel="noreferrer" class="inline-flex min-h-10 items-center hover:text-ink">{m.settings_about_docs()}</a>
		<a href={RELEASES_URL} target="_blank" rel="noreferrer" class="inline-flex min-h-10 items-center hover:text-ink">{m.settings_about_releases()}</a>
	</footer>
</div>
