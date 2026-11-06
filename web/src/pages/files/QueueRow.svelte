<script lang="ts">
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import ArrowUp from '@lucide/svelte/icons/arrow-up';
	import FolderOpen from '@lucide/svelte/icons/folder-open';
	import GripVertical from '@lucide/svelte/icons/grip-vertical';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import { fmtAbsolute, fmtBytes, fmtDuration, pct } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { uploadLook } from '../../lib/status.ts';
	import Button from '../../lib/ui/Button.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import type { FileLike } from '../../lib/upload/queue.ts';
	import { type UploadView, uploads } from '../../lib/upload/store.svelte.ts';

	interface Props {
		item: UploadView;
		position: number;
		ondragstart: () => void;
		ondragend: () => void;
	}

	let { item, position, ondragstart, ondragend }: Props = $props();

	const name = $derived(prefs.file(item.filename));
	const look = $derived(uploadLook(item.status));
	const percent = $derived(pct(item.sentBytes, item.size));
	const movable = $derived(item.status === 'queued' || item.status === 'paused' || item.status === 'error' || item.status === 'needs_file');
	const active = $derived(item.status === 'starting' || item.status === 'uploading');
	let picker = $state<HTMLInputElement>();
</script>

<div class="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 py-2">
	<span
		class={`hidden size-10 shrink-0 items-center justify-center text-ink-3 sm:inline-flex ${movable ? 'cursor-grab' : 'opacity-30'}`}
		draggable={movable}
		role="presentation"
		ondragstart={(e) => {
			e.dataTransfer?.setData('text/plain', item.filename);
			ondragstart();
		}}
		{ondragend}
	>
		<GripVertical size={14} />
	</span>
	<span class="w-6 shrink-0 text-center font-mono text-xs text-ink-3">{position}</span>
	<div class="min-w-0 flex-1 basis-56">
		<div class="flex items-center gap-2">
			<span class="truncate font-mono text-xs font-medium" title={name} data-testid="queue-name">{name}</span>
			<Plate tone={look.tone} icon={look.icon} spin={look.spin} label={look.label} class="shrink-0" />
		</div>
		<div class="my-1"><Progress value={item.status === 'done' ? 100 : percent} tone={item.status === 'error' ? 'warning' : item.status === 'done' ? 'signal' : 'ink'} label={name} /></div>
		<p class="m-0 text-xs text-ink-3">
			<span title={m.files_queue_modified({ date: fmtAbsolute(new Date(item.lastModified)) })}>{m.files_queue_progress({ sent: fmtBytes(item.sentBytes), size: fmtBytes(item.size), percent })}</span>
			{#if active && item.rate > 0}<span> {m.files_queue_rate({ rate: fmtBytes(item.rate), left: fmtDuration(item.eta) })}</span>{/if}
			{#if item.retrying}<span class="text-advisory-ink"> {m.files_queue_retrying()}</span>{/if}
		</p>
		{#if item.error}<p class="m-0 font-mono text-xs text-warning-ink">{item.error}</p>{/if}
	</div>
	<div class="flex shrink-0 items-center">
		{#if movable}
			<IconButton icon={ArrowUp} label={m.files_queue_move_up({ name })} onclick={() => uploads.move(item.key, -1)} />
			<IconButton icon={ArrowDown} label={m.files_queue_move_down({ name })} onclick={() => uploads.move(item.key, 1)} />
		{/if}
		{#if active || item.status === 'queued'}
			<IconButton icon={Pause} label={m.files_queue_pause({ name })} onclick={() => uploads.pause(item.key)} />
		{/if}
		{#if (item.status === 'paused' || item.status === 'error') && item.hasFile}
			<IconButton icon={Play} label={m.files_queue_resume({ name })} onclick={() => uploads.resume(item.key)} />
		{/if}
		{#if item.status === 'needs_file'}
			<Button icon={FolderOpen} onclick={() => picker?.click()}>{m.files_queue_select_file()}</Button>
			<input
				bind:this={picker}
				class="sr-only"
				type="file"
				tabindex="-1"
				aria-label={m.files_queue_select_file()}
				onchange={(e) => {
					const file = e.currentTarget.files?.[0];
					e.currentTarget.value = '';
					if (file) uploads.add([file as FileLike], 'size-asc');
				}}
			/>
		{/if}
		{#if item.status === 'done'}
			<IconButton icon={X} label={m.files_queue_dismiss({ name })} onclick={() => uploads.dismiss(item.key)} />
		{:else if item.status !== 'completing'}
			<IconButton icon={X} tone="danger" label={m.files_queue_cancel({ name })} onclick={() => uploads.cancel(item.key)} />
		{/if}
	</div>
</div>
