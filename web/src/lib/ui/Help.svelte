<script lang="ts">
	/**
	 * A small question mark next to a label, for the rare setting whose name alone could be misread.
	 * The text shows on mouse hover and on keyboard focus; a tap toggles it. The popover is placed against
	 * the nearest positioned parent (a settings row is one), so it never runs off a narrow screen.
	 */
	import CircleHelp from '@lucide/svelte/icons/circle-help';
	import { m } from '#lib/paraglide/messages.js';

	let { text, label }: { text: string; label?: string } = $props();
	const id = $props.id();
	let open = $state(false);
	let hovered = false;
</script>

<button
	type="button"
	class="-my-2 -ml-2 inline-flex size-10 shrink-0 items-center justify-center rounded-btn text-ink-3 hover:text-ink"
	aria-label={label ?? m.settings_help()}
	aria-expanded={open}
	aria-describedby={open ? id : undefined}
	onclick={(e) => {
		e.preventDefault();
		e.stopPropagation();
		open = hovered || !open;
	}}
	onpointerenter={(e) => {
		if (e.pointerType === 'mouse') open = hovered = true;
	}}
	onpointerleave={(e) => {
		if (e.pointerType === 'mouse') open = hovered = false;
	}}
	onfocus={(e) => {
		if (e.currentTarget.matches(':focus-visible')) open = true;
	}}
	onblur={() => (open = false)}
	onkeydown={(e) => {
		if (e.key === 'Escape' && open) {
			e.preventDefault();
			e.stopPropagation();
			open = false;
		}
	}}
>
	<CircleHelp size={14} aria-hidden="true" />
</button>
{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<span
		{id}
		role="tooltip"
		class="absolute top-full right-3 left-3 z-20 -mt-1 block rounded-btn border border-line-strong bg-surface-2 px-3 py-2 text-xs font-normal text-ink-2 shadow-float"
		onclick={(e) => e.preventDefault()}
	>
		{text}
	</span>
{/if}
