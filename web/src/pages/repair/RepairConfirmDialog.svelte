<script lang="ts">
	/** Asks before a repair: how many messages in how many mailboxes, from which analysis, and where the originals go. */
	import { m } from '#lib/paraglide/messages.js';
	import type { RepairBox } from '../../lib/api/types.ts';
	import { toDate } from '../../lib/format.ts';
	import Button from '../../lib/ui/Button.svelte';
	import { clock } from '../../lib/ui/clock.svelte.ts';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import RelTime from '../../lib/ui/RelTime.svelte';

	interface Props {
		/** The mailboxes that will be repaired: each has an analysis that found damage. */
		boxes: RepairBox[];
		/** Mailboxes of the selection left out for lack of a damaged analysis. */
		skipped?: number;
		onconfirm: () => unknown;
		onclose: () => void;
	}

	let { boxes, skipped = 0, onconfirm, onclose }: Props = $props();
	let busy = $state(false);

	const messages = $derived(boxes.reduce((n, b) => n + (b.last_scan?.progress.affected ?? 0), 0));
	/** The oldest analysis is the one that may have drifted most. */
	const oldest = $derived(
		boxes.reduce<number | null>((min, b) => {
			const t = toDate(b.last_scan?.finished_at ?? b.last_scan?.created_at)?.getTime() ?? null;
			return t != null && (min == null || t < min) ? t : min;
		}, null)
	);
	const stale = $derived(oldest != null && clock.now - oldest > 24 * 3600 * 1000);

	async function run() {
		busy = true;
		try {
			await onconfirm();
		} finally {
			busy = false;
			onclose();
		}
	}
</script>

<Dialog title={m.repair_confirm_repair_title()} {onclose} center>
	<div class="flex flex-col gap-3" data-testid="repair-confirm">
		<p class="m-0 font-semibold" data-testid="repair-confirm-counts">{m.repair_confirm_repair_summary({ count: messages, boxes: boxes.length })}</p>
		{#if oldest != null}
			<p class="m-0 text-sm text-ink-2">{m.repair_confirm_scanned()} <RelTime value={oldest} /></p>
		{/if}
		{#if stale}<Notice>{m.repair_confirm_stale()}</Notice>{/if}
		<p class="m-0">{m.repair_confirm_repair_intro()}</p>
		<Notice tone="note">{m.repair_confirm_originals()}</Notice>
		{#if skipped}<p class="m-0 text-sm text-ink-2">{m.repair_confirm_skipped({ count: skipped })}</p>{/if}
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" loading={busy} onclick={run}>{m.repair_action_repair()}</Button>
	{/snippet}
</Dialog>
