<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import { uploads } from '../../lib/upload/store.svelte.ts';

	function setConcurrency(value: number) {
		prefs.set('uploadConcurrency', value);
		uploads.setConcurrency(value);
	}
</script>

<SettingRow label={m.settings_tuning_concurrency()} data-testid="section-tuning">
	<select class="input" value={String(prefs.value.uploadConcurrency)} onchange={(e) => setConcurrency(Number(e.currentTarget.value))} data-testid="upload-concurrency">
		{#each [1, 2, 3, 4] as value (value)}
			<option value={String(value)}>{value}</option>
		{/each}
	</select>
</SettingRow>
