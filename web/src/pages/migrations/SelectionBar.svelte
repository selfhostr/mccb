<script lang="ts">
	/**
	 * Fixed to the bottom: it covers nothing that the page has not reserved room for, and
	 * pushes nothing. It says what is hidden and that actions leave it alone.
	 */
	import Ban from '@lucide/svelte/icons/ban';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Pause from '@lucide/svelte/icons/pause';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Play from '@lucide/svelte/icons/play';
	import RotateCw from '@lucide/svelte/icons/rotate-cw';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { BulkJobAction } from '../../lib/logic/bulk.ts';
	import Button from '../../lib/ui/Button.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';

	interface Props {
		targets: number;
		hidden: number;
		testing: number;
		/** The job actions that would change something for the selected mailboxes, and the one running now. */
		jobActions: BulkJobAction[];
		busy: BulkJobAction | null;
		onjob: (action: BulkJobAction) => void;
		ontest: () => void;
		onimport: () => void;
		onedit: () => void;
		ondelete: () => void;
		onclear: () => void;
		ondropHidden: () => void;
		/** The open panel takes the right of the screen: the bar stops beside it when there is room. */
		besidePanel: boolean;
	}

	let { targets, hidden, testing, jobActions, busy, onjob, ontest, onimport, onedit, ondelete, onclear, ondropHidden, besidePanel }: Props = $props();

	const LABEL = $derived<Record<BulkJobAction, string>>({
		pause: m.jobs_action_pause(),
		resume: m.jobs_action_resume(),
		stop: m.jobs_action_stop(),
		'retry-errors': m.jobs_action_retry_errors()
	});
	const ICON = { pause: Pause, resume: Play, stop: Ban, 'retry-errors': RotateCw };
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
			<Button icon={Pencil} disabled={!targets} onclick={onedit} data-testid="bulk-edit">{m.migrations_bar_edit()}</Button>
			<Button icon={Trash2} disabled={!targets} onclick={ondelete} data-testid="bulk-delete">{m.migrations_bar_delete()}</Button>
			{#each jobActions as action (action)}
				<Button variant={action === 'stop' ? 'danger' : 'secondary'} icon={ICON[action]} loading={busy === action} disabled={busy != null} onclick={() => onjob(action)} data-testid={`bulk-${action}`}>{LABEL[action]}</Button>
			{/each}
			<Button variant="primary" icon={Play} disabled={!targets} onclick={onimport}>{m.migrations_bar_import()}</Button>
			<IconButton icon={X} label={m.migrations_bar_clear()} onclick={onclear} />
		</div>
	</div>
</div>
