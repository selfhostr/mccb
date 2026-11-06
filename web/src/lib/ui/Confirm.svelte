<script lang="ts">
	import { m } from '#lib/paraglide/messages.js';
	import Button from './Button.svelte';
	import Dialog from './Dialog.svelte';

	interface Props {
		title: string;
		text: string;
		confirm: string;
		danger?: boolean;
		onconfirm: () => unknown;
		onclose: () => void;
	}

	let { title, text, confirm, danger = false, onconfirm, onclose }: Props = $props();
	let busy = $state(false);

	async function run() {
		busy = true;
		try {
			await onconfirm();
		} finally {
			busy = false;
			onclose();
		}
	}
</script>

<Dialog {title} {onclose}>
	<p class="m-0">{text}</p>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant={danger ? 'danger' : 'primary'} loading={busy} onclick={run}>{confirm}</Button>
	{/snippet}
</Dialog>
