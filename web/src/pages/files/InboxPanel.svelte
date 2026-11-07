<script lang="ts">
	/** The drop folder: where to copy files, and what is waiting there. Hidden when the folder is off or the route is missing. */
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { get } from '../../lib/api/client.ts';
	import type { InboxListing } from '../../lib/api/types.ts';
	import { fmtBytes, fmtDuration } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';

	let listing = $state.raw<InboxListing | null>(null);

	async function refresh() {
		try {
			const next = await get<InboxListing>('/intake/inbox');
			// Same content: keep the same object, nothing re-renders.
			if (JSON.stringify(next) !== JSON.stringify(listing)) listing = next;
		} catch {
			listing = null;
		}
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => void refresh(), 5000);
		return () => clearInterval(timer);
	});
</script>

{#if listing?.enabled}
	<div data-testid="inbox">
		<Disclosure title={m.files_inbox_title()} summary={m.files_inbox_waiting({ count: listing.files.length })}>
			<div class="flex flex-col gap-2">
				<p class="m-0 text-ink-2">{m.files_inbox_sentence({ seconds: listing.settle_seconds })}</p>
				<div class="flex items-center gap-1">
					<code class="min-w-0 truncate rounded-plate bg-surface-2 px-2 py-1 font-mono text-xs" data-testid="inbox-path">{listing.path}</code>
					<CopyButton text={listing.path} label={m.files_inbox_copy()} />
				</div>
				{#if listing.files.length}
					<ul class="m-0 flex list-none flex-col p-0" data-testid="inbox-files">
						{#each listing.files as file (file.name)}
							<li class="flex flex-wrap items-baseline gap-x-3 border-b border-line py-1.5">
								<span class="min-w-0 flex-1 basis-48 truncate font-mono text-xs" title={prefs.file(file.name)}>{prefs.file(file.name)}</span>
								<span class="text-xs text-ink-3">{fmtBytes(file.size)}</span>
								<span class="text-xs text-ink-3">{m.files_inbox_changed({ ago: fmtDuration(file.modified_ago) })}</span>
							</li>
						{/each}
					</ul>
				{:else}
					<p class="m-0 text-xs text-ink-3">{m.files_inbox_empty()}</p>
				{/if}
			</div>
		</Disclosure>
	</div>
{/if}
