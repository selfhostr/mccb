<script lang="ts">
	/**
	 * The notes of a mirror comparison or run: what is worth knowing without being a block, one line each.
	 * A soft or permanent deletion is a warning (shown before Apply), the others are plain notes.
	 * The message of the API is kept under the label; an unknown code shows that message as its label.
	 */
	import { m } from '#lib/paraglide/messages.js';
	import type { MirrorNote } from '../../lib/api/types.ts';
	import { type NoteLabels, noteLines } from '../../lib/logic/mirror.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Notice from '../../lib/ui/Notice.svelte';

	let { notes, prefix = 'mirror-note' }: { notes: readonly MirrorNote[] | null | undefined; prefix?: string } = $props();

	const LABELS = $derived<NoteLabels>({
		soft_delete: { label: m.migrations_mirror_note_soft_delete(), hint: m.migrations_mirror_note_soft_delete_hint() },
		hard_delete: { label: m.migrations_mirror_note_hard_delete(), hint: m.migrations_mirror_note_hard_delete_hint() },
		no_copy_kept: { label: m.migrations_mirror_note_no_copy_kept(), hint: m.migrations_mirror_note_no_copy_kept_hint() },
		shared_resource: { label: m.migrations_mirror_note_shared_resource(), hint: m.migrations_mirror_note_shared_resource_hint() },
		target_modified: { label: m.migrations_mirror_note_target_modified(), hint: m.migrations_mirror_note_target_modified_hint() }
	});
	const lines = $derived(noteLines(notes, LABELS));
</script>

{#each lines as line, i (i)}
	<Notice tone={line.tone === 'warning' ? 'advisory' : 'note'}>
		<div data-testid={`${prefix}-${line.code}`} data-tone={line.tone}>
			<p class={`m-0 ${line.tone === 'warning' ? 'font-semibold' : 'font-medium text-ink'}`}>{prefs.text(line.label)}</p>
			{#if line.hint}<p class="m-0 mt-1">{line.hint}</p>{/if}
			{#if line.detail}<p class="m-0 mt-1 font-mono break-words text-ink-2">{prefs.text(line.detail)}</p>{/if}
		</div>
	</Notice>
{/each}
