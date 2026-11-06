<script lang="ts">
	/** The two marks of the static demo: a badge in the top bar, a sentence and the install link under the page. */
	import ExternalLink from '@lucide/svelte/icons/external-link';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import { m } from '#lib/paraglide/messages.js';
	import Plate from '../lib/ui/Plate.svelte';

	let { part }: { part: 'badge' | 'footer' } = $props();

	const INSTALL = 'https://mccb.open.yety.ch/docs/install/';
</script>

{#if part === 'badge'}
	<span class="min-w-0" data-testid="demo-badge" title={m.demo_badge_title()}>
		<Plate tone="advisory" icon={FlaskConical} label={m.demo_badge_label()} />
	</span>
{:else}
	<footer class="mx-auto flex max-w-[1160px] flex-wrap items-center gap-x-4 px-[var(--gutter)] pb-10 text-xs text-ink-2" data-testid="demo-footer">
		<p class="m-0 py-2">{m.demo_footer_text()}</p>
		<a class="inline-flex min-h-10 items-center gap-1.5 text-ink underline" href={INSTALL} target="_blank" rel="noopener" data-testid="demo-install">
			{m.demo_footer_install()}
			<ExternalLink size={13} />
		</a>
	</footer>
{/if}
