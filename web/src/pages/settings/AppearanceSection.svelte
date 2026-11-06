<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { COLUMN_KEYS, type ColumnKey, prefs } from '../../lib/prefs.svelte.ts';
	import Field from '../../lib/ui/Field.svelte';
	import LanguageSelect from '../../lib/ui/LanguageSelect.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';

	const COLUMN_LABELS = $derived<Record<ColumnKey, string>>({
		server: m.migrations_col_server(),
		connection: m.migrations_col_connection(),
		pst: m.migrations_col_pst(),
		content: m.migrations_col_content(),
		progress: m.migrations_col_progress()
	});
</script>

<section id="appearance" class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid="section-appearance">
	<h2 class="text-[0.9375rem]">{m.settings_appearance_title()}</h2>
	<p class="m-0 mt-1 mb-2 text-ink-2">{m.settings_appearance_sentence()}</p>
	<div class="flex flex-col gap-3">
		<div class="grid max-w-[640px] grid-cols-1 gap-3 sm:grid-cols-3">
			<Field label={m.settings_appearance_language()}>
				<LanguageSelect />
			</Field>
			<Field label={m.settings_appearance_theme()}>
				<select class="input" value={prefs.value.theme} onchange={(e) => prefs.set('theme', e.currentTarget.value as 'dark' | 'light')} data-testid="theme">
					<option value="dark">{m.settings_appearance_dark()}</option>
					<option value="light">{m.settings_appearance_light()}</option>
				</select>
			</Field>
			<Field label={m.settings_appearance_density()}>
				<select class="input" value={prefs.value.density} onchange={(e) => prefs.set('density', e.currentTarget.value as 'compact' | 'comfortable')} data-testid="density">
					<option value="compact">{m.settings_appearance_compact()}</option>
					<option value="comfortable">{m.settings_appearance_comfortable()}</option>
				</select>
			</Field>
		</div>
		<Toggle checked={prefs.value.maskAddresses} label={m.settings_appearance_mask()} hint={m.settings_appearance_mask_hint()} onchange={(v) => prefs.set('maskAddresses', v)} />
		<details>
			<summary class="flex min-h-10 cursor-pointer items-center font-medium text-ink-2">{m.settings_appearance_columns()}</summary>
			<div class="flex flex-col">
				{#each COLUMN_KEYS as key (key)}
					<Toggle checked={prefs.value.columns[key]} label={COLUMN_LABELS[key]} onchange={(v) => prefs.set('columns', { ...prefs.value.columns, [key]: v })} />
				{/each}
			</div>
		</details>
	</div>
</section>
