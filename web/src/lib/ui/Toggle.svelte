<script lang="ts">
	/** A labelled on/off choice. The native checkbox stays, so keyboard and forms just work. */
	interface Props {
		checked: boolean;
		label: string;
		hint?: string;
		onchange: (checked: boolean) => void;
	}

	let { checked, label, hint, onchange }: Props = $props();
</script>

<label class="check justify-start">
	<input type="checkbox" {checked} onchange={(e) => onchange(e.currentTarget.checked)} />
	<span>
		{label}
		{#if hint}<small class="block text-xs text-ink-3">{hint}</small>{/if}
	</span>
</label>
