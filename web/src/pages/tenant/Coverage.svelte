<script lang="ts">
	/** Mail, calendars, contacts, tasks: four small icons, each with a mark and a sentence for what will not go. */
	import Calendar from '@lucide/svelte/icons/calendar';
	import Contact from '@lucide/svelte/icons/contact';
	import ListTodo from '@lucide/svelte/icons/list-todo';
	import Mail from '@lucide/svelte/icons/mail';
	import { m } from '#lib/paraglide/messages.js';
	import type { Coverage } from '../../lib/api/types.ts';
	import { COVERAGE_KINDS } from '../../lib/logic/tenantmatch.ts';
	import { coverageLook, kindName } from './looks.ts';

	let { coverage }: { coverage: Coverage | null | undefined } = $props();

	const ICONS = { mail: Mail, calendar: Calendar, contacts: Contact, tasks: ListTodo };
</script>

<ul class="m-0 flex list-none items-center gap-1.5 p-0" aria-label={m.tenant_cov_label()} data-testid="tenant-coverage">
	{#each COVERAGE_KINDS as kind (kind)}
		{@const Icon = ICONS[kind]}
		{@const state = coverage?.[kind] ?? 'unknown'}
		{@const look = coverageLook(state)}
		{@const text = m.tenant_cov_item({ kind: kindName(kind), state: look.label })}
		<li class={`relative inline-flex size-6 items-center justify-center ${look.tone}`} title={text} data-kind={kind} data-cov={state}>
			<Icon size={16} aria-hidden="true" />
			{#if look.mark}
				{@const Mark = look.mark}
				<span class="absolute -right-0.5 -bottom-0.5 inline-flex rounded-full bg-surface" aria-hidden="true"><Mark size={10} strokeWidth={3} /></span>
			{/if}
			<span class="sr-only">{text}</span>
		</li>
	{/each}
</ul>
