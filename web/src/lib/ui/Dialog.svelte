<script lang="ts">
	/**
	 * Native modal dialog: focus trap, Esc and the inert background come from the browser.
	 * Mount it when needed; closing calls `onclose` and the parent unmounts it.
	 */
	import X from '@lucide/svelte/icons/x';
	import type { Snippet } from 'svelte';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';

	interface Props {
		title: string;
		children: Snippet;
		actions?: Snippet;
		wide?: boolean;
		onclose: () => void;
	}

	let { title, children, actions, wide = false, onclose }: Props = $props();
	let dialog = $state<HTMLDialogElement>();

	onMount(() => {
		dialog?.showModal();
	});
</script>

<dialog
	bind:this={dialog}
	class={`m-auto max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] rounded-card border border-line-strong bg-surface p-0 shadow-float ${wide ? 'max-w-[680px]' : 'max-w-[460px]'}`}
	aria-label={title}
	{onclose}
>
	<div class="flex items-center justify-between gap-2 border-b border-line py-1 pr-1 pl-4">
		<h2 class="min-w-0 text-[0.9375rem]">{title}</h2>
		<button type="button" class="inline-flex size-10 shrink-0 items-center justify-center text-ink-2 hover:text-ink" aria-label={m.common_close()} onclick={() => dialog?.close()}>
			<X size={16} />
		</button>
	</div>
	<div class="p-4">{@render children()}</div>
	{#if actions}<div class="flex flex-wrap justify-end gap-2 border-t border-line p-3">{@render actions()}</div>{/if}
</dialog>
