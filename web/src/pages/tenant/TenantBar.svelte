<script lang="ts">
	/** Fixed to the bottom, like the selection bar of Migrations: what is ticked, what will go, and where. */
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Play from '@lucide/svelte/icons/play';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';

	interface Props {
		count: number;
		/** Words of the kinds at least one ticked mailbox gets, in order. */
		kinds: string[];
		server: string;
		hidden: number;
		testing: boolean;
		canResync: boolean;
		ontest: () => void;
		onresync: () => void;
		onstart: () => void;
		onshowHidden: () => void;
	}

	let { count, kinds, server, hidden, testing, canResync, ontest, onresync, onstart, onshowHidden }: Props = $props();

	const list = $derived.by(() => {
		try {
			return new Intl.ListFormat(document.documentElement.lang || undefined, { type: 'conjunction' }).format(kinds);
		} catch {
			return kinds.join(', ');
		}
	});
</script>

<div class="fixed right-0 bottom-0 left-[var(--side-w)] z-30 border-t border-line-strong bg-surface" role="region" aria-label={m.tenant_bar_label()} data-testid="tenant-bar">
	<div class="mx-auto flex max-w-[1160px] flex-wrap items-center gap-x-3 gap-y-1 px-[var(--gutter)] py-2">
		<div class="min-w-0 flex-1 basis-56" aria-live="polite">
			<p class="m-0 font-medium" data-testid="tenant-bar-summary">
				{count ? (kinds.length ? m.tenant_bar_summary({ count, kinds: list, server }) : m.tenant_bar_summary_nothing({ count, server })) : m.tenant_bar_none()}
			</p>
			{#if hidden}
				<p class="m-0 text-xs text-advisory-ink">
					{m.tenant_bar_hidden({ count: hidden })}
					<button type="button" class="inline-flex min-h-10 items-center px-1 underline" onclick={onshowHidden}>{m.tenant_bar_show_all()}</button>
				</p>
			{/if}
		</div>
		<div class="flex flex-wrap items-center gap-2">
			<Button variant="ghost" icon={FlaskConical} disabled={!count} loading={testing} onclick={ontest} data-testid="tenant-test">{m.tenant_bar_test()}</Button>
			<Button icon={RefreshCw} disabled={!canResync} onclick={onresync} title={canResync ? undefined : m.tenant_bar_resync_hint()} data-testid="tenant-resync">{m.tenant_bar_resync()}</Button>
			<Button variant="primary" icon={Play} disabled={!count} onclick={onstart} data-testid="tenant-start">{m.tenant_bar_start()}</Button>
		</div>
	</div>
</div>
