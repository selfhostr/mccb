<script lang="ts">
	/**
	 * Native modal dialog: focus trap, Esc and the inert background come from the browser.
	 * It opens as a panel on the right edge, full height; `center` keeps the small box in the
	 * middle, for a yes/no question. Mount it when needed; closing calls `onclose`.
	 * On open the focus goes to an element marked `data-autofocus`, else to the first control of
	 * the content, else to the first action; the close button only when there is nothing else.
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
		center?: boolean;
		onclose: () => void;
	}

	let { title, children, actions, wide = false, center = false, onclose }: Props = $props();
	let dialog = $state<HTMLDialogElement>();
	let body = $state<HTMLElement>();
	let footer = $state<HTMLElement>();

	const FOCUSABLE = 'input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), a[href], summary, [tabindex]:not([tabindex="-1"])';

	function first(scope: HTMLElement | undefined): HTMLElement | null | undefined {
		return [...(scope?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])].find((el) => el.getClientRects().length > 0);
	}

	onMount(() => {
		dialog?.showModal();
		const target = dialog?.querySelector<HTMLElement>('[data-autofocus]:not([disabled])') ?? first(body) ?? first(footer);
		target?.focus();
	});
</script>

<dialog
	bind:this={dialog}
	class={center
		? 'm-auto max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] max-w-[440px] rounded-card border border-line-strong bg-surface p-0 shadow-float'
		: `drawer my-0 mr-0 ml-auto h-dvh max-h-dvh w-full rounded-none border-0 border-l border-line-strong bg-surface p-0 shadow-float ${wide ? 'max-w-[720px]' : 'max-w-[480px]'}`}
	aria-label={title}
	{onclose}
>
	<div class={center ? 'flex flex-col' : 'flex h-full flex-col'}>
		<div class="flex min-h-14 shrink-0 items-center justify-between gap-2 border-b border-line pr-2 pl-5">
			<h2 class="min-w-0 truncate text-base">{title}</h2>
			<button type="button" class="inline-flex size-10 shrink-0 items-center justify-center rounded-btn text-ink-2 transition-colors duration-200 hover:bg-ghost hover:text-ink" aria-label={m.common_close()} title={m.common_close()} onclick={() => dialog?.close()}>
				<X size={16} />
			</button>
		</div>
		<div bind:this={body} class={center ? 'p-5' : 'min-h-0 flex-1 overflow-y-auto p-5'}>{@render children()}</div>
		{#if actions}<div bind:this={footer} class="flex shrink-0 flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{@render actions()}</div>{/if}
	</div>
</dialog>
