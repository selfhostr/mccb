<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Field from '../../lib/ui/Field.svelte';
	import { uploads } from '../../lib/upload/store.svelte.ts';

	function setConcurrency(value: number) {
		prefs.set('uploadConcurrency', value);
		uploads.setConcurrency(value);
	}
</script>

<section id="tuning" class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid="section-tuning">
	<h2 class="text-[0.9375rem]">{m.settings_tuning_title()}</h2>
	<p class="m-0 mt-1 mb-2 text-ink-2">{m.settings_tuning_sentence()}</p>
	<div class="max-w-[320px]">
		<Field label={m.settings_tuning_concurrency()} hint={m.settings_tuning_concurrency_hint()}>
			<select class="input" value={String(prefs.value.uploadConcurrency)} onchange={(e) => setConcurrency(Number(e.currentTarget.value))} data-testid="upload-concurrency">
				{#each [1, 2, 3, 4] as value (value)}
					<option value={String(value)}>{value}</option>
				{/each}
			</select>
		</Field>
	</div>
</section>
