<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Field from '../../lib/ui/Field.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import { uploads } from '../../lib/upload/store.svelte.ts';

	function setConcurrency(value: number) {
		prefs.set('uploadConcurrency', value);
		uploads.setConcurrency(value);
	}
</script>

<Section id="tuning" title={m.settings_tuning_title()} sentence={m.settings_tuning_sentence()}>
	<div class="max-w-[460px]">
		<Field label={m.settings_tuning_concurrency()} hint={m.settings_tuning_concurrency_hint()}>
			<select class="input" value={String(prefs.value.uploadConcurrency)} onchange={(e) => setConcurrency(Number(e.currentTarget.value))} data-testid="upload-concurrency">
				{#each [1, 2, 3, 4] as value (value)}
					<option value={String(value)}>{value}</option>
				{/each}
			</select>
		</Field>
	</div>
</Section>
