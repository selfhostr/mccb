<script lang="ts">
	/**
	 * The drop folder: where to copy files, and what is waiting there. Off, it says so with a link to
	 * the setting; unreachable, it says why. A server without the route (404) shows nothing.
	 */
	import FolderInput from '@lucide/svelte/icons/folder-input';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText, get } from '../../lib/api/client.ts';
	import type { InboxListing } from '../../lib/api/types.ts';
	import { fmtBytes, fmtDuration } from '../../lib/format.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';

	let listing = $state.raw<InboxListing | null>(null);
	let failure = $state<string | null>(null);

	async function refresh() {
		try {
			const next = await get<InboxListing>('/intake/inbox');
			// Same content: keep the same object, nothing re-renders.
			if (JSON.stringify(next) !== JSON.stringify(listing)) listing = next;
			failure = null;
		} catch (error) {
			listing = null;
			// A server without a drop folder at all answers 404: nothing to offer then.
			failure = error instanceof ApiError && error.status === 404 ? null : errorText(error);
		}
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => void refresh(), 5000);
		return () => clearInterval(timer);
	});
</script>

{#if failure}
	<p class="m-0 flex min-h-10 flex-wrap items-center gap-x-2 text-xs text-advisory-ink" role="status" data-testid="inbox-failed">
		<FolderInput size={14} class="shrink-0" />
		<span class="min-w-0 break-words">{m.files_inbox_failed({ error: failure })}</span>
		<a class="inline-flex min-h-10 items-center text-ink-2 underline hover:text-ink" href="/settings#intake">{m.files_inbox_settings()}</a>
	</p>
{:else if listing && !listing.enabled}
	<p class="m-0 flex min-h-10 flex-wrap items-center gap-x-2 text-xs text-ink-2" data-testid="inbox-off">
		<FolderInput size={14} class="shrink-0 text-ink-3" />
		<span>{m.files_inbox_off()}</span>
		<a class="inline-flex min-h-10 items-center underline hover:text-ink" href="/settings#intake">{m.files_inbox_turn_on()}</a>
	</p>
{:else if listing?.enabled}
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
