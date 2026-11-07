<script lang="ts">
	/** The empty page says the three steps of a migration, which one is next, and has its button. */
	import Check from '@lucide/svelte/icons/check';
	import FileUp from '@lucide/svelte/icons/file-up';
	import Plus from '@lucide/svelte/icons/plus';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';

	interface Props {
		hasServer: boolean;
		onserver: () => void;
		onnew: () => void;
		oncsv: () => void;
	}

	let { hasServer, onserver, onnew, oncsv }: Props = $props();

	const current = $derived(hasServer ? 1 : 0);
	const STEPS = $derived([
		{ title: m.migrations_steps_server(), hint: m.migrations_steps_server_hint() },
		{ title: m.migrations_steps_new(), hint: m.migrations_steps_new_hint() },
		{ title: m.migrations_steps_start(), hint: m.migrations_steps_start_hint() }
	]);
</script>

<section class="max-w-[760px] rounded-card border border-line bg-surface p-6" data-testid="empty-state">
	<h2 class="text-lg">{hasServer ? m.migrations_empty_no_account_title() : m.migrations_empty_no_server_title()}</h2>
	<p class="m-0 mt-1 text-ink-2">{hasServer ? m.migrations_empty_no_account_text() : m.migrations_empty_no_server_text()}</p>
	<ol class="m-0 mt-5 flex list-none flex-col gap-5 p-0">
		{#each STEPS as step, i (i)}
			<li class="flex gap-3" aria-current={i === current ? 'step' : undefined}>
				<span
					class={`flex size-8 shrink-0 items-center justify-center rounded-full border font-semibold ${i < current ? 'border-transparent bg-surface-2 text-signal-ink' : i === current ? 'border-ink text-ink' : 'border-line-strong text-ink-3'}`}
				>
					{#if i < current}<Check size={16} aria-label={m.migrations_step_ready()} />{:else}{i + 1}{/if}
				</span>
				<div class="flex min-w-0 flex-1 flex-col gap-2 pt-1">
					<p class={`m-0 font-semibold ${i > current ? 'text-ink-2' : ''}`}>{step.title}</p>
					<p class="m-0 text-ink-2">{step.hint}</p>
					{#if i === 0 && i === current}
						<div><Button variant="primary" icon={Plus} onclick={onserver}>{m.migrations_empty_no_server_action()}</Button></div>
					{:else if i === 1 && i === current}
						<div class="flex flex-wrap gap-2">
							<Button variant="primary" icon={Plus} onclick={onnew}>{m.migrations_add_label()}</Button>
							<Button icon={FileUp} onclick={oncsv}>{m.migrations_add_csv()}</Button>
						</div>
					{/if}
				</div>
			</li>
		{/each}
	</ol>
</section>
