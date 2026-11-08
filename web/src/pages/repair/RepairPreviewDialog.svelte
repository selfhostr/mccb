<script lang="ts">
	/** One damaged message as it is and as the repair would write it. Reads only. */
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, post } from '../../lib/api/client.ts';
	import type { RepairExample, RepairPreview, RepairSummary } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { issueLabel } from './issues.ts';

	let { boxId, example, onclose }: { boxId: number; example: RepairExample; onclose: () => void } = $props();

	let preview = $state.raw<RepairPreview | null>(null);
	let error = $state<string | null>(null);

	$effect(() => {
		post<RepairPreview>(`/repair/boxes/${boxId}/preview`, { folder: example.folder, uid: example.uid }).then(
			(p) => (preview = p),
			(err) => (error = errorText(err))
		);
	});
</script>

{#snippet side(title: string, s: RepairSummary)}
	<section class="flex min-w-0 flex-1 basis-72 flex-col gap-2">
		<h3 class="m-0 text-sm font-medium text-ink-2">{title}</h3>
		<p class="m-0 font-medium">{prefs.text(s.subject) || m.repair_no_subject()}</p>
		<pre class="m-0 max-h-72 overflow-auto rounded-plate bg-surface-2 p-2 font-mono text-xs whitespace-pre-wrap">{prefs.text(s.text)}</pre>
		{#if s.parts.length}
			<ul class="m-0 flex list-none flex-col gap-0.5 p-0 font-mono text-xs">
				{#each s.parts as part, i (i)}
					<li>{prefs.file(part.name) || '-'} <span class="text-ink-3">{part.type}{part.inline ? ` · ${m.repair_preview_inline()}` : ''}</span></li>
				{/each}
			</ul>
		{/if}
	</section>
{/snippet}

<Dialog title={m.repair_preview_title()} {onclose} wide>
	{#if error}
		<ErrorNotice title={m.repair_load_failed()} message={error} />
	{:else if !preview}
		<Skeleton />
	{:else}
		<div class="flex flex-col gap-3" data-testid="repair-preview-dialog">
			{#if preview.error}<ErrorNotice title={m.repair_preview_refused()} message={preview.error} />{/if}
			{#if preview.already}<Notice>{m.repair_preview_already()}</Notice>{/if}
			{#if preview.left.length}<Notice>{m.repair_preview_left({ issues: preview.left.map(issueLabel).join(', ') })}</Notice>{/if}
			<div class="flex flex-wrap gap-4">
				{@render side(m.repair_preview_before(), preview.before)}
				{#if preview.after}
					{@render side(m.repair_preview_after(), preview.after)}
				{:else if !preview.error && !preview.already}
					<p class="m-0 flex-1 basis-72 text-ink-2">{m.repair_preview_nothing()}</p>
				{/if}
			</div>
		</div>
	{/if}
	{#snippet actions()}
		<Button onclick={onclose}>{m.common_close()}</Button>
	{/snippet}
</Dialog>
