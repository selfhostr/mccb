<script lang="ts">
	/**
	 * A list of mailboxes for the new migration form, in one place: pasted in the box, dropped on it or chosen
	 * as a .csv/.txt file. Everything is read here, in the browser; the rows it reads fill the form's rows,
	 * where they can still be changed, and the lines it skips are listed with their reason. The template has
	 * exactly the columns the form shows. "Undo" takes the rows of the last import away while none was sent.
	 */
	import Download from '@lucide/svelte/icons/download';
	import FileText from '@lucide/svelte/icons/file-text';
	import FileUp from '@lucide/svelte/icons/file-up';
	import ListPlus from '@lucide/svelte/icons/list-plus';
	import Undo2 from '@lucide/svelte/icons/undo-2';
	import { m } from '#lib/paraglide/messages.js';
	import { decodeText, type ListColumn, type ListError, type ListRow, parseMailboxList, templateCsv } from '../../lib/logic/mailboxList.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import Help from '../../lib/ui/Help.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	interface Props {
		/** Every column a list may give, with its label for the messages. */
		columns: (ListColumn & { label: string })[];
		/** The columns the form shows now: the template and the example. */
		shown: ListColumn[];
		/** Addresses already in the form or on the destination, lowercased. */
		taken: string[];
		disabled?: boolean;
		/** The last import can still be taken back. */
		undoable: boolean;
		onfill: (rows: ListRow[]) => void;
		onundo: () => void;
	}

	let { columns, shown, taken, disabled = false, undoable, onfill, onundo }: Props = $props();

	const MAX_BYTES = 5 * 1024 * 1024;
	const SHOWN_ERRORS = 20;

	let open = $state(false);
	let text = $state('');
	let over = $state(false);
	let input = $state<HTMLInputElement>();
	let result = $state<{ count: number; errors: ListError[]; file: string | null } | null>(null);
	let fileError = $state<string | null>(null);

	const template = $derived(templateCsv(shown));
	const label = (key?: string) => columns.find((c) => c.key === key)?.label ?? key ?? '';
	const reason = (e: ListError) => (e.reason === 'email' ? m.migrations_list_err_email() : e.reason === 'duplicate' ? m.migrations_list_err_duplicate() : m.migrations_list_err_missing({ column: label(e.column) }));

	function read(source: string, file: string | null): boolean {
		fileError = null;
		const parsed = parseMailboxList(source, columns, taken);
		result = { count: parsed.rows.length, errors: parsed.errors, file };
		if (parsed.rows.length) onfill(parsed.rows);
		return parsed.rows.length > 0;
	}

	function fillText() {
		if (read(text, null)) text = '';
	}

	async function readFile(file: File | undefined) {
		if (!file) return;
		const ok = /\.(csv|txt|tsv)$/i.test(file.name) || /^text\//.test(file.type);
		if (!ok || file.size > MAX_BYTES) {
			result = null;
			fileError = file.name;
			return;
		}
		try {
			read(decodeText(await file.arrayBuffer()), file.name);
		} catch {
			result = null;
			fileError = file.name;
		}
	}

	const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes('Files');

	function ondragover(e: DragEvent) {
		if (!hasFiles(e) || disabled) return;
		e.preventDefault();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
		over = true;
	}

	function ondragleave(e: DragEvent) {
		if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) over = false;
	}

	function ondrop(e: DragEvent) {
		if (!hasFiles(e) || disabled) return;
		e.preventDefault();
		over = false;
		void readFile(e.dataTransfer?.files[0]);
	}

	function download() {
		// A BOM, so a spreadsheet opens the accents right.
		const url = URL.createObjectURL(new Blob(['﻿', template], { type: 'text/csv;charset=utf-8' }));
		const a = document.createElement('a');
		a.href = url;
		a.download = 'mailboxes.csv';
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	}

	function undo() {
		onundo();
		result = null;
	}
</script>

<Disclosure title={m.migrations_list_title()} bind:open data-testid="new-list">
	<div class="flex flex-col gap-3">
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative" {ondragover} {ondragleave} {ondrop}>
			<textarea
				class={`input block min-h-28 w-full font-mono text-xs ${over ? 'border-ink' : ''}`}
				aria-label={m.migrations_list_title()}
				placeholder={m.migrations_list_placeholder()}
				bind:value={text}
				spellcheck="false"
				autocomplete="off"
				{disabled}
				onkeydown={(e) => {
					if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && text.trim()) {
						e.preventDefault();
						fillText();
					}
				}}
				data-testid="new-list-text"
			></textarea>
			<div
				class={`pointer-events-none absolute inset-0 flex items-center justify-center gap-2 rounded-btn border border-dashed border-ink bg-surface/90 text-sm font-medium text-ink transition-opacity duration-[var(--dur-2)] ease-[var(--ease)] ${over ? 'opacity-100' : 'opacity-0'}`}
				aria-hidden="true"
			>
				<FileUp size={18} />{m.migrations_list_drop()}
			</div>
		</div>

		<div class="flex flex-wrap items-center gap-2">
			<Button icon={ListPlus} onclick={fillText} disabled={!text.trim() || disabled} data-testid="new-list-fill">{m.migrations_list_fill()}</Button>
			<Button variant="ghost" icon={FileUp} onclick={() => input?.click()} {disabled} data-testid="new-list-file">{m.migrations_list_choose()}</Button>
			<input
				bind:this={input}
				class="sr-only"
				type="file"
				accept=".csv,.txt,.tsv,text/csv,text/plain"
				tabindex="-1"
				aria-hidden="true"
				onchange={(e) => {
					const el = e.currentTarget;
					void readFile(el.files?.[0]).finally(() => (el.value = ''));
				}}
			/>
		</div>

		<div class="relative flex flex-col gap-1">
			<div class="flex items-center gap-2">
				<button type="button" class="inline-flex min-h-10 items-center gap-2 text-ink-2 hover:text-ink" onclick={download} data-testid="new-list-template">
					<Download size={14} />{m.migrations_list_template()}
				</button>
				<Help text={m.migrations_list_format()} />
			</div>
			<code class="block overflow-x-auto rounded-plate bg-surface-2 p-2 font-mono text-xs whitespace-pre" data-testid="new-list-format">{template.trimEnd()}</code>
		</div>

		<div class="flex flex-col gap-2" aria-live="polite">
			{#if fileError}
				<p class="m-0 text-xs text-warning-ink" role="alert">{m.migrations_list_err_file()} <span class="font-mono">{fileError}</span></p>
			{:else if result}
				<div class="appear flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="new-list-result">
					{#if result.file}<span class="inline-flex min-w-0 items-center gap-1 font-mono text-xs text-ink-2"><FileText size={14} class="shrink-0" /><span class="truncate">{result.file}</span></span>{/if}
					<span class={`text-sm ${result.count ? 'text-ink' : 'text-warning-ink'}`}>
						{!result.count && !result.errors.length ? m.migrations_list_none() : result.errors.length ? m.migrations_list_read_skipped({ count: result.count, skipped: result.errors.length }) : m.migrations_list_read({ count: result.count })}
					</span>
					{#if undoable && result.count}
						<Button variant="ghost" icon={Undo2} onclick={undo} data-testid="new-list-undo">{m.migrations_list_undo()}</Button>
					{/if}
				</div>
				{#if result.errors.length}
					<Notice>
						<ul class="m-0 list-none p-0 font-mono text-xs" data-testid="new-list-errors">
							{#each result.errors.slice(0, SHOWN_ERRORS) as e (e.line)}
								<li>{m.migrations_csv_line({ line: e.line, message: reason(e) })}</li>
							{/each}
							{#if result.errors.length > SHOWN_ERRORS}<li>{m.migrations_import_more({ count: result.errors.length - SHOWN_ERRORS })}</li>{/if}
						</ul>
					</Notice>
				{/if}
			{/if}
		</div>
	</div>
</Disclosure>
