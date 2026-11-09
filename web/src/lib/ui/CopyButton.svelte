<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import Copy from '@lucide/svelte/icons/copy';
	import { m } from '#lib/paraglide/messages.js';
	import { copyText } from '../clipboard.ts';
	import IconButton from './IconButton.svelte';
	import { toast } from './toast.svelte.ts';

	let { text, label }: { text: string | (() => string); label?: string } = $props();
	let done = $state(false);
	let timer: ReturnType<typeof setTimeout> | undefined;

	async function copy() {
		const ok = await copyText(typeof text === 'function' ? text() : text);
		if (!ok) {
			toast(m.common_copy_failed(), 'warning');
			return;
		}
		done = true;
		clearTimeout(timer);
		timer = setTimeout(() => (done = false), 2000);
	}
</script>

<IconButton icon={done ? Check : Copy} label={done ? m.common_copied() : (label ?? m.common_copy())} onclick={copy} />
