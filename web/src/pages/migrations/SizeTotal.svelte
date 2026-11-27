<script lang="ts">
	/** The size of the mail at the source: "≈" for an estimate, "calculating…" while measured, "-" when unknown. */
	import { m } from '#lib/paraglide/messages.js';
	import type { SourceSize } from '../../lib/api/types.ts';
	import { DASH, fmtBytes } from '../../lib/format.ts';

	let { size }: { size: SourceSize | undefined } = $props();
</script>

{#if size?.bytes != null}
	<span title={size.approx ? (size.basis === 'file' ? m.migrations_size_approx_file() : m.migrations_size_approx()) : undefined} data-testid="size-total">{size.approx ? '≈ ' : ''}{fmtBytes(size.bytes)}</span>
{:else if size?.computing}
	<span class="text-ink-3" data-testid="size-total" data-computing>{m.migrations_size_computing()}</span>
{:else}
	<span data-testid="size-total">{DASH}</span>
{/if}
