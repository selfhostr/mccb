<script lang="ts">
	/** The empty page says the three steps of a migration and which one is next. Only the first has a button: the others are the + of the header. */
	import Check from '@lucide/svelte/icons/check';
	import Plus from '@lucide/svelte/icons/plus';
	import { m } from '#lib/paraglide/messages.js';
	import Button from '../../lib/ui/Button.svelte';

	let { hasServer, onserver }: { hasServer: boolean; onserver: () => void } = $props();

	const current = $derived(hasServer ? 1 : 0);
	const STEPS = $derived([
		{ title: m.migrations_steps_server(), hint: m.migrations_steps_server_hint() },
		{ title: m.migrations_steps_new(), hint: m.migrations_steps_new_hint() },
		{ title: m.migrations_steps_start(), hint: '' }
	]);
</script>

<section class="rounded-card border border-line bg-surface p-5" data-testid="empty-state">
	<h2 class="text-base">{hasServer ? m.migrations_empty_no_account_title() : m.migrations_empty_no_server_title()}</h2>
	<ol class="m-0 mt-4 grid list-none gap-x-6 gap-y-4 p-0 md:grid-cols-3">
		{#each STEPS as step, i (i)}
			<li class="flex gap-3" aria-current={i === current ? 'step' : undefined}>
				<span
					class={`flex size-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold ${i < current ? 'border-transparent bg-surface-2 text-signal-ink' : i === current ? 'border-ink text-ink' : 'border-line-strong text-ink-3'}`}
				>
					{#if i < current}<Check size={14} aria-label={m.migrations_step_ready()} />{:else}{i + 1}{/if}
				</span>
				<div class="flex min-w-0 flex-1 flex-col gap-2 pt-1">
					<p class={`m-0 font-semibold ${i === current ? '' : 'text-ink-2'}`}>{step.title}</p>
					{#if i === current}
						<p class="m-0 text-ink-2">{step.hint}</p>
						{#if i === 0}<div><Button variant="primary" icon={Plus} onclick={onserver}>{m.migrations_empty_no_server_action()}</Button></div>{/if}
					{/if}
				</div>
			</li>
		{/each}
	</ol>
</section>
