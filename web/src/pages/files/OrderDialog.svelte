<script lang="ts">
	/** Several files at once: the user picks the upload order and sees it before anything starts. */
	import Upload from '@lucide/svelte/icons/upload';
	import { m } from '#lib/paraglide/messages.js';
	import { fmtAbsolute, fmtBytes } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import { type FileLike, sortFiles, type UploadOrder, UPLOAD_ORDERS } from '../../lib/upload/queue.ts';
	import { uploads } from '../../lib/upload/store.svelte.ts';

	let { files, onclose }: { files: File[]; onclose: () => void } = $props();

	let order = $state<UploadOrder>('size-asc');
	const sorted = $derived(sortFiles(files, order));
	const LABELS = $derived<Record<UploadOrder, string>>({
		'size-asc': m.files_order_size_asc(),
		'size-desc': m.files_order_size_desc(),
		date: m.files_order_date(),
		name: m.files_order_name()
	});

	function start() {
		uploads.add(files as FileLike[], order);
		onclose();
	}
</script>

<Dialog title={m.files_order_title({ count: files.length })} {onclose} wide>
	<div class="flex flex-col gap-3">
		<fieldset class="m-0 flex flex-col border-0 p-0">
			<legend class="mb-1 p-0 font-medium text-ink-2">{m.files_order_legend()}</legend>
			{#each UPLOAD_ORDERS as value (value)}
				<label class="check justify-start">
					<input type="radio" name="upload-order" {value} bind:group={order} />
					<span>{LABELS[value]}</span>
				</label>
			{/each}
		</fieldset>
		<p class="m-0 text-xs text-ink-3">{m.files_order_date_note()}</p>
		<ol class="m-0 max-h-56 list-inside list-decimal overflow-y-auto rounded-btn border border-line p-2" data-testid="order-preview">
			{#each sorted as file (`${file.name}|${file.size}|${file.lastModified}`)}
				<li class="py-0.5">
					<span class="font-mono text-xs">{prefs.file(file.name)}</span>
					<span class="text-xs text-ink-3" title={fmtAbsolute(new Date(file.lastModified))}>{fmtBytes(file.size)}</span>
				</li>
			{/each}
		</ol>
	</div>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" icon={Upload} onclick={start} data-testid="order-start">{m.files_order_start()}</Button>
	{/snippet}
</Dialog>
