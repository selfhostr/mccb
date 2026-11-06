<script lang="ts">
	/**
	 * A write-only field. The server never returns a secret, only that one is set: an untouched field keeps it,
	 * typing replaces it, "Clear" removes it. The parent reads `value` and `clear` and decides what to send.
	 */
	import { m } from '#lib/paraglide/messages.js';
	import Button from './Button.svelte';

	interface Props {
		label: string;
		hasSecret: boolean;
		value?: string;
		clear?: boolean;
		area?: boolean;
		hint?: string;
		required?: boolean;
		autocomplete?: string;
		[key: string]: unknown;
	}

	let { label, hasSecret, value = $bindable(''), clear = $bindable(false), area = false, hint, required = false, autocomplete = 'new-password', ...rest }: Props = $props();
	const id = $props.id();
	const stored = $derived(hasSecret && !value);
</script>

<div class="flex min-w-0 flex-col gap-1">
	<label for={id} class="font-medium text-ink-2">{label}</label>
	<div class="flex items-start gap-2">
		{#if clear}
			<p class="m-0 flex min-h-10 min-w-0 flex-1 items-center text-ink-2" id={id}>{m.common_secret_will_clear()}</p>
			<Button variant="ghost" onclick={() => (clear = false)}>{m.common_secret_keep()}</Button>
		{:else}
			{#if area}
				<textarea {id} class="input min-h-24 font-mono text-xs" rows="4" spellcheck="false" placeholder={stored ? m.common_secret_saved() : ''} bind:value {...rest}></textarea>
			{:else}
				<input {id} class="input" type="password" {autocomplete} required={required && !stored} placeholder={stored ? m.common_secret_saved() : ''} bind:value {...rest} />
			{/if}
			{#if stored}<Button variant="ghost" onclick={() => (clear = true)}>{m.common_secret_clear()}</Button>{/if}
		{/if}
	</div>
	{#if hint}<small class="text-xs text-ink-3">{hint}</small>{/if}
</div>
