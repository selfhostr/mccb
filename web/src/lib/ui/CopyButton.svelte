<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import Copy from '@lucide/svelte/icons/copy';
	import { m } from '#lib/paraglide/messages.js';
	import IconButton from './IconButton.svelte';

	let { text, label }: { text: string | (() => string); label?: string } = $props();
	let done = $state(false);

	async function copy() {
		const value = typeof text === 'function' ? text() : text;
		try {
			await navigator.clipboard.writeText(value);
		} catch {
			// Plain HTTP has no clipboard API: the old way still works there.
			const area = document.createElement('textarea');
			area.value = value;
			document.body.append(area);
			area.select();
			document.execCommand('copy');
			area.remove();
		}
		done = true;
		setTimeout(() => (done = false), 1200);
	}
</script>

<IconButton icon={done ? Check : Copy} label={done ? m.common_copied() : (label ?? m.common_copy())} onclick={copy} />
