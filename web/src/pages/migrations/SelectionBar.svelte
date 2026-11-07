<script lang="ts">
	/**
	 * Fixed to the bottom: it covers nothing that the page has not reserved room for, and
	 * pushes nothing. It says what is hidden and that actions leave it alone.
	 */
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Play from '@lucide/svelte/icons/play';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';

	interface Props {
		targets: number;
		hidden: number;
		testing: number;
		ontest: () => void;
		onimport: () => void;
		onclear: () => void;
		ondropHidden: () => void;
		/** The open panel takes the right of the screen: the bar stops beside it when there is room. */
		besidePanel: boolean;
	}

	let { targets, hidden, testing, ontest, onimport, onclear, ondropHidden, besidePanel }: Props = $props();
</script>

<div class={`fixed right-0 bottom-0 left-[var(--side-w)] z-30 border-t border-line-strong bg-surface ${besidePanel ? 'min-[1100px]:right-[600px]' : ''}`} data-testid="selection-bar" role="region" aria-label={m.migrations_bar_label()}>
	<div class="mx-auto flex max-w-[1160px] flex-wrap items-center gap-x-3 gap-y-1 px-[var(--gutter)] py-2">
		<div class="min-w-0 flex-1 basis-56" aria-live="polite">
			<p class="m-0 font-medium" data-testid="selection-count">{m.migrations_bar_selected({ count: targets })}</p>
			{#if hidden}
				<p class="m-0 text-xs text-advisory-ink" data-testid="selection-hidden">
					{m.migrations_bar_hidden({ count: hidden })}
					<button type="button" class="inline-flex min-h-10 items-center px-1 underline" onclick={ondropHidden}>{m.migrations_bar_drop_hidden()}</button>
				</p>
			{/if}
		</div>
		<div class="flex flex-wrap items-center gap-2">
			<Button icon={FlaskConical} disabled={!targets} loading={testing > 0} onclick={ontest}>
				{testing > 0 ? m.migrations_bar_testing({ count: testing }) : m.migrations_bar_test()}
			</Button>
			<Button variant="primary" icon={Play} disabled={!targets} onclick={onimport}>{m.migrations_bar_import()}</Button>
			<IconButton icon={X} label={m.migrations_bar_clear()} onclick={onclear} />
		</div>
	</div>
</div>
