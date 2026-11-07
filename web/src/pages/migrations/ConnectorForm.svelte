<script lang="ts">
	/**
	 * Draws the fields of one connector role from the catalogue. It knows field types, not kinds:
	 * a kind added later on the server shows up here with no change. Names, labels and help are shown as served.
	 * No native `required`: a field in a closed disclosure would block the submit without a word. The editor checks.
	 */
	import type { ConnectorRole } from '../../lib/api/types.ts';
	import type { SecretDrafts, Values } from '../../lib/logic/connector.ts';
	import Field from '../../lib/ui/Field.svelte';
	import SecretInput from '../../lib/ui/SecretInput.svelte';
	import Toggle from '../../lib/ui/Toggle.svelte';

	interface Props {
		role: ConnectorRole;
		values: Values;
		drafts: SecretDrafts;
		hasSecret: boolean;
	}

	let { role, values = $bindable(), drafts = $bindable(), hasSecret }: Props = $props();
</script>

<div class="flex flex-col gap-3" data-testid="connector-fields">
	{#each role.fields as field (field.name)}
		{#if field.secret}
			<SecretInput label={field.label} hint={field.help} {hasSecret} bind:value={drafts[field.name].value} bind:clear={drafts[field.name].clear} data-field={field.name} />
		{:else if field.type === 'boolean'}
			<Toggle checked={values[field.name] === true} label={field.label} hint={field.help} onchange={(v) => (values[field.name] = v)} />
		{:else if field.type === 'select' && field.options?.length}
			<Field label={field.label} hint={field.help}>
				<select class="input" bind:value={values[field.name]} data-field={field.name}>
					{#if !field.required && (field.default == null || field.default === '')}<option value=""></option>{/if}
					{#each field.options as option (option)}
						<option value={option}>{option}</option>
					{/each}
				</select>
			</Field>
		{:else if field.type === 'number'}
			<Field label={field.label} hint={field.help}>
				<input class="input font-mono" type="number" bind:value={values[field.name]} data-field={field.name} />
			</Field>
		{:else}
			<!-- text, url, a password that is not secret, and any type the UI has never seen -->
			<Field label={field.label} hint={field.help}>
				<input
					class="input font-mono"
					type={field.type === 'url' ? 'url' : field.type === 'password' ? 'password' : 'text'}
					autocomplete="off"
					bind:value={values[field.name]}
					data-field={field.name}
				/>
			</Field>
		{/if}
	{/each}
</div>
