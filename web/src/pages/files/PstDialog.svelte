<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import type { Pst } from '../../lib/api/types.ts';
	import { fmtNum } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Notice from '../../lib/ui/Notice.svelte';

	let { pst, onclose }: { pst: Pst; onclose: () => void } = $props();

	const folders = $derived(pst.scan?.folders ?? []);
	const warnings = $derived((pst.scan?.errors ?? []).slice(0, 50).map((e) => (typeof e === 'string' ? e : JSON.stringify(e))));
</script>

<Dialog title={prefs.file(pst.filename)} {onclose} wide>
	<div class="flex flex-col gap-3">
		<table class="w-full text-xs">
			<thead>
				<tr class="text-left text-ink-2">
					<th class="py-1 font-medium">{m.files_details_folder()}</th>
					<th class="py-1 font-medium">{m.files_details_type()}</th>
					<th class="py-1 text-right font-medium">{m.files_details_items()}</th>
				</tr>
			</thead>
			<tbody>
				{#each folders as folder, i (i)}
					<tr class="border-t border-line">
						<td class="py-1 font-mono break-words">{Array.isArray(folder.path) ? folder.path.join(' / ') : folder.path}</td>
						<td class="py-1">{folder.kind}</td>
						<td class="py-1 text-right font-mono">{fmtNum(folder.count)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
		{#if warnings.length}
			<Notice>
				<p class="m-0 font-medium">{m.files_details_warnings()}</p>
				<ul class="m-0 list-none p-0 font-mono text-xs">
					{#each warnings as warning, i (i)}<li>{warning}</li>{/each}
				</ul>
			</Notice>
		{/if}
	</div>
</Dialog>
