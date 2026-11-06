<script lang="ts">
	/** Browser time, relative when recent, the absolute date on hover. */
	import { fmtAbsolute, fmtRelative, toDate } from '../format.ts';
	import { clock } from './clock.svelte.ts';

	let { value }: { value: number | string | null | undefined } = $props();
	const date = $derived(toDate(value));
</script>

<time datetime={date?.toISOString()} title={fmtAbsolute(date)}>{fmtRelative(date, clock.now)}</time>
