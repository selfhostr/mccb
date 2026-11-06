<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { availableLocales, i18n, LANGUAGE_NAMES } from '../i18n.svelte.ts';
</script>

<select class="input" aria-label={m.settings_appearance_language()} value={i18n.locale} onchange={(e) => i18n.change(e.currentTarget.value)} data-testid="language">
	{#each availableLocales as locale (locale)}
		<option value={locale} lang={locale}>{LANGUAGE_NAMES[locale] ?? locale}</option>
	{/each}
</select>
