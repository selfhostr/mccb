<script lang="ts">
	import Check from '@lucide/svelte/icons/circle-check';
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import Info from '@lucide/svelte/icons/info';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import { toasts } from './toast.svelte.ts';

	const ICONS = { signal: Check, info: Info, advisory: CircleAlert, warning: TriangleAlert };
	const TEXT = { signal: 'text-signal-ink', info: 'text-info-ink', advisory: 'text-advisory-ink', warning: 'text-warning-ink' };
</script>

<!-- Top right, over the content: it never moves anything. -->
<div class="pointer-events-none fixed top-[calc(var(--bar-h)+8px)] right-3 z-50 flex w-[min(360px,calc(100vw-24px))] flex-col gap-2" aria-live="polite">
	{#each toasts.list as t (t.id)}
		{@const Icon = ICONS[t.tone]}
		<div class="pointer-events-auto flex items-start gap-2 rounded-btn border border-line-strong bg-surface-2 py-1 pr-1 pl-3 shadow-float" role="status" data-testid="toast">
			<span class={`mt-2.5 ${TEXT[t.tone]}`}><Icon size={14} /></span>
			<p class="m-0 min-w-0 flex-1 py-2">{t.message}</p>
			<button type="button" class="inline-flex size-10 shrink-0 items-center justify-center text-ink-2 hover:text-ink" aria-label={m.common_close()} onclick={() => toasts.dismiss(t.id)}>
				<X size={14} />
			</button>
		</div>
	{/each}
</div>
