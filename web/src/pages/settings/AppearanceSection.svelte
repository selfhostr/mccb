<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import LanguageSelect from '../../lib/ui/LanguageSelect.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';
</script>

<Section id="appearance" title={m.settings_appearance_title()} sentence={m.settings_appearance_sentence()}>
	<div class="flex max-w-[460px] flex-col gap-3">
		<Field label={m.settings_appearance_language()}>
			<LanguageSelect />
		</Field>
		<Field label={m.settings_appearance_theme()}>
			<select class="input" value={prefs.value.theme} onchange={(e) => prefs.set('theme', e.currentTarget.value as 'dark' | 'light')} data-testid="theme">
				<option value="dark">{m.settings_appearance_dark()}</option>
				<option value="light">{m.settings_appearance_light()}</option>
			</select>
		</Field>
		<Disclosure title={m.settings_appearance_more()} data-testid="appearance-more">
			<div class="flex flex-col gap-3">
				<Field label={m.settings_appearance_density()}>
					<select class="input" value={prefs.value.density} onchange={(e) => prefs.set('density', e.currentTarget.value as 'compact' | 'comfortable')} data-testid="density">
						<option value="compact">{m.settings_appearance_compact()}</option>
						<option value="comfortable">{m.settings_appearance_comfortable()}</option>
					</select>
				</Field>
				<Toggle checked={prefs.value.maskAddresses} label={m.settings_appearance_mask()} hint={m.settings_appearance_mask_hint()} onchange={(v) => prefs.set('maskAddresses', v)} />
			</div>
		</Disclosure>
	</div>
</Section>
