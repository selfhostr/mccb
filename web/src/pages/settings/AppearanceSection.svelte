<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import LanguageSelect from '../../lib/ui/LanguageSelect.svelte';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
</script>

<div class="divide-y divide-line" data-testid="section-appearance">
	<SettingRow label={m.settings_appearance_language()}>
		<LanguageSelect />
	</SettingRow>
	<SettingRow label={m.settings_appearance_theme()}>
		<select class="input" value={prefs.value.theme} onchange={(e) => prefs.set('theme', e.currentTarget.value as 'dark' | 'light')} data-testid="theme">
			<option value="dark">{m.settings_appearance_dark()}</option>
			<option value="light">{m.settings_appearance_light()}</option>
		</select>
	</SettingRow>
	<SettingRow label={m.settings_appearance_density()}>
		<select class="input" value={prefs.value.density} onchange={(e) => prefs.set('density', e.currentTarget.value as 'compact' | 'comfortable')} data-testid="density">
			<option value="compact">{m.settings_appearance_compact()}</option>
			<option value="comfortable">{m.settings_appearance_comfortable()}</option>
		</select>
	</SettingRow>
	<SettingRow label={m.settings_appearance_mask()} help={m.settings_appearance_mask_hint()}>
		<span class="check"><input class="switch" type="checkbox" checked={prefs.value.maskAddresses} onchange={(e) => prefs.set('maskAddresses', e.currentTarget.checked)} data-testid="mask-addresses" /></span>
	</SettingRow>
</div>
