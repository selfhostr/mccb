<script lang="ts">
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import { flip } from 'svelte/animate';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';
	import { reducedMotion, rowFade } from '../../lib/ui/motion.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';
	import QueueRow from './QueueRow.svelte';

	let dragged = $state<string | null>(null);

	const waiting = $derived(uploads.items.filter((i) => i.status === 'queued' || i.status === 'paused' || i.status === 'needs_file').length);
	const sending = $derived(uploads.items.filter((i) => i.status === 'starting' || i.status === 'uploading' || i.status === 'completing').length);
	const paused = $derived(uploads.items.filter((i) => i.status === 'paused').length);
	const done = $derived(uploads.items.filter((i) => i.status === 'done').length);
	const missing = $derived(uploads.items.filter((i) => i.status === 'needs_file').length);
</script>

{#if uploads.items.length}
	<section class="rounded-card border border-line-strong bg-surface" aria-label={m.files_queue_label()} data-testid="queue">
		<div class="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
			<p class="m-0 font-medium" data-testid="queue-sentence">{m.files_queue_sentence({ sending, waiting })}</p>
			<div class="flex flex-wrap gap-2">
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
			<p class="m-0 border-b border-line px-3 py-2 text-advisory-ink">{m.files_queue_reselect()}</p>
		{/if}
		<ol class="m-0 list-none p-0">
			{#each uploads.items as item, index (item.key)}
				<li
					class={`border-b border-line last:border-b-0 ${dragged === item.key ? 'opacity-50' : ''}`}
					animate:flip={{ duration: reducedMotion() ? 0 : 180 }}
					transition:rowFade
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
					<QueueRow {item} position={index + 1} ondragstart={() => (dragged = item.key)} ondragend={() => (dragged = null)} />
				</li>
			{/each}
		</ol>
	</section>
{/if}
