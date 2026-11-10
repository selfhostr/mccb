<script lang="ts">
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import ArrowUp from '@lucide/svelte/icons/arrow-up';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FolderOpen from '@lucide/svelte/icons/folder-open';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import { fmtAbsolute, fmtBytes, fmtDuration, pct } from '../../lib/format.ts';
	import { kindOf } from '../../lib/logic/pst.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { uploadLook } from '../../lib/status.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Progress from '../../lib/ui/Progress.svelte';
	import type { FileLike } from '../../lib/upload/queue.ts';
	import { type UploadView, uploads } from '../../lib/upload/store.svelte.ts';

	let { item }: { item: UploadView } = $props();

	const name = $derived(prefs.file(item.filename));
	const look = $derived(uploadLook(item.status));
	const percent = $derived(item.status === 'done' ? 100 : pct(item.sentBytes, item.size));
	const movable = $derived(item.status === 'queued' || item.status === 'paused' || item.status === 'error' || item.status === 'needs_file');
	const active = $derived(item.status === 'starting' || item.status === 'uploading');
	const canPause = $derived(active || item.status === 'queued');
	const canResume = $derived((item.status === 'paused' || item.status === 'error') && item.hasFile);
	let picker = $state<HTMLInputElement>();

	const menu = $derived([
		...(movable
			? [
					{ label: m.migrations_row_queue_up(), icon: ArrowUp, onselect: () => uploads.move(item.key, -1) },
					{ label: m.migrations_row_queue_down(), icon: ArrowDown, onselect: () => uploads.move(item.key, 1) }
				]
			: []),
		...(canPause ? [{ label: m.jobs_action_pause(), icon: Pause, onselect: () => uploads.pause(item.key) }] : []),
		...(canResume ? [{ label: m.jobs_action_resume(), icon: Play, onselect: () => uploads.resume(item.key) }] : []),
		...(item.status === 'done'
			? [{ label: m.files_queue_remove(), icon: X, onselect: () => uploads.dismiss(item.key) }]
			: item.status !== 'completing'
				? [{ label: m.common_cancel(), icon: X, danger: true, onselect: () => uploads.cancel(item.key) }]
				: [])
	]);
</script>

<div class="file-name">
	<p class="m-0 truncate font-medium" title={name} data-testid="queue-name">{name}</p>
	<p class="m-0 text-xs text-ink-2">
		<span title={m.files_queue_modified({ date: fmtAbsolute(new Date(item.lastModified)) })}>{m.files_queue_progress({ sent: fmtBytes(item.sentBytes), size: fmtBytes(item.size), percent })}</span>
		{#if active && item.rate > 0}<span> {m.files_queue_rate({ rate: fmtBytes(item.rate), left: fmtDuration(item.eta) })}</span>{/if}
		{#if item.retrying}<span class="text-advisory-ink"> {m.files_queue_retrying()}</span>{/if}
	</p>
	{#if item.error}<p class="m-0 font-mono text-xs text-warning-ink">{item.error}</p>{/if}
</div>
<span class="file-kind text-xs text-ink-2">{kindOf(item.filename)}</span>
<span class="file-size text-xs text-ink-2">{fmtBytes(item.size)}</span>
<div class="file-status"><Plate tone={look.tone} icon={look.icon} label={look.label} /></div>
<div class="file-bar flex items-center gap-3">
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
	{:else}
		<span class="min-w-0 flex-1"><Progress value={percent} tone={item.status === 'error' ? 'warning' : 'ink'} label={name} /></span>
		<span class="w-11 shrink-0 text-right font-mono text-xs text-ink-2">{percent}%</span>
	{/if}
</div>
<div class="file-menu">
	{#if menu.length}<Menu label={m.files_list_more({ name })} icon={EllipsisVertical} items={menu} />{/if}
</div>
