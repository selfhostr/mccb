<script lang="ts">
	/** The uploads: the first rows of the Files panel, under a line that speaks for all of them. */
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import QueueRow from './QueueRow.svelte';

	let dragged = $state<string | null>(null);

	const waiting = $derived(uploads.items.filter((i) => i.status === 'queued' || i.status === 'paused' || i.status === 'needs_file').length);
	const sending = $derived(uploads.items.filter((i) => i.status === 'starting' || i.status === 'uploading' || i.status === 'completing').length);
	const paused = $derived(uploads.items.filter((i) => i.status === 'paused').length);
	const done = $derived(uploads.items.filter((i) => i.status === 'done').length);
	const missing = $derived(uploads.items.filter((i) => i.status === 'needs_file').length);

	const movable = (status: string) => status === 'queued' || status === 'paused' || status === 'error' || status === 'needs_file';
</script>

{#if uploads.items.length}
	<section aria-label={m.files_queue_label()} data-testid="queue">
		<div class="row-head pl-4">
			<p class="m-0" data-testid="queue-sentence">{m.files_queue_sentence({ sending, waiting })}</p>
			<div class="flex flex-wrap gap-1">
				{#if sending || waiting > paused + missing}
					<Button variant="ghost" icon={Pause} onclick={uploads.pauseAll}>{m.files_queue_pause_all()}</Button>
				{/if}
				{#if paused}
					<Button variant="ghost" icon={Play} onclick={uploads.resumeAll}>{m.files_queue_resume_all()}</Button>
				{/if}
				{#if done}
					<Button variant="ghost" onclick={uploads.clearDone}>{m.files_queue_clear_done()}</Button>
				{/if}
			</div>
		</div>
		{#if missing}
			<p class="m-0 border-t border-line px-4 py-2 text-advisory-ink">{m.files_queue_reselect()}</p>
		{/if}
		<ol class="row-list">
			{#each uploads.items as item (item.key)}
				<!-- Dragging is the quick way to reorder; the row menu does the same by keyboard and on phones. -->
				<li
					class={`file-row ${movable(item.status) ? 'cursor-grab' : ''} ${dragged === item.key ? 'opacity-50' : ''}`}
					draggable={movable(item.status)}
					ondragstart={(e) => {
						e.dataTransfer?.setData('text/plain', item.filename);
						dragged = item.key;
					}}
					ondragend={() => (dragged = null)}
					ondragover={(e) => {
						if (dragged) e.preventDefault();
					}}
					ondrop={(e) => {
						e.preventDefault();
						if (dragged && dragged !== item.key) uploads.moveTo(dragged, item.key);
						dragged = null;
					}}
					data-upload={item.filename}
				>
					<QueueRow {item} />
				</li>
			{/each}
		</ol>
	</section>
{/if}
