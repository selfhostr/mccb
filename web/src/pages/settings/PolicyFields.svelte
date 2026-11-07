<script lang="ts">
	/**
	 * Folder mapping, filters and, on a server, allowed hours and bandwidth: behind one disclosure, loaded when it
	 * opens. The dialog that holds it asks `dirty()` and `body()` when it saves.
	 */
	import Plus from '@lucide/svelte/icons/plus';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { PolicyDoc, Policy } from '../../lib/api/types.ts';
	import { fmtWeekday } from '../../lib/format.ts';
	import { effectiveLines, emptyPolicyForm, type PolicyForm, policyBody, policyChanged, policyToForm } from '../../lib/logic/policy.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';

	interface Props {
		/** GET/PUT address of the policy, or null for something not created yet. */
		url: string | null;
		server: boolean;
	}

	let { url, server }: Props = $props();

	let open = $state(false);
	let form = $state<PolicyForm>(emptyPolicyForm());
	let initial = emptyPolicyForm();
	let loaded = $state(false);
	let error = $state<string | null>(null);
	let effective = $state.raw<Policy | undefined>();
	let started = false;

	async function load() {
		error = null;
		try {
			const doc = await get<PolicyDoc>(url as string);
			initial = policyToForm(doc.policy);
			form = policyToForm(doc.policy);
			effective = doc.effective;
			loaded = true;
		} catch (e) {
			error = errorText(e);
			started = false;
		}
	}

	$effect(() => {
		if (open && !started) {
			started = true;
			if (url) void load();
			else loaded = true;
		}
	});

	/** True when something was changed here: only then the dialog writes the policy. */
	export const dirty = (): boolean => loaded && policyChanged(form, initial);
	export const body = (): Record<string, unknown> => policyBody($state.snapshot(form), server);

	const LABELS = $derived<Record<string, string>>({
		map: m.policy_map(),
		exclude: m.policy_exclude(),
		dateFrom: m.policy_date_from(),
		dateTo: m.policy_date_to(),
		maxSize: m.policy_max_size(),
		window: m.policy_hours(),
		rate: m.policy_rate()
	});
	const lines = $derived(effectiveLines(effective, (key) => LABELS[key] ?? key, fmtWeekday));

	function toggleDay(day: number) {
		form.days = form.days.includes(day) ? form.days.filter((d) => d !== day) : [...form.days, day];
	}
</script>

<Disclosure title={m.policy_title()} bind:open data-testid="policy">
	{#if error}
		<ErrorNotice title={m.policy_load_failed()} message={error} onretry={load} />
	{:else if !loaded}
		<Skeleton class="h-24" />
	{:else}
		<div class="flex flex-col gap-3">
			{#if lines.length}
				<div data-testid="policy-effective">
					<p class="m-0 font-medium text-ink-2">{m.policy_effective()}</p>
					<dl class="m-0 mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs">
						{#each lines as line (line.key)}
							<dt class="text-ink-3">{line.label}</dt>
							<dd class="m-0 font-mono break-words">{line.value}</dd>
						{/each}
					</dl>
				</div>
			{/if}

			<fieldset class="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
				<legend class="mb-1 p-0 font-medium text-ink-2">{m.policy_map()}</legend>
				{#each form.map as rule (rule)}
					<div class="flex items-end gap-2">
						<label class="flex min-w-0 flex-1 flex-col gap-1">
							<span class="text-xs text-ink-3">{m.policy_map_source()}</span>
							<input class="input font-mono" autocomplete="off" bind:value={rule.source} data-testid="policy-map-source" />
						</label>
						<label class="flex min-w-0 flex-1 flex-col gap-1">
							<span class="text-xs text-ink-3">{m.policy_map_target()}</span>
							<input class="input font-mono" autocomplete="off" placeholder="@sent" bind:value={rule.target} data-testid="policy-map-target" />
						</label>
						<IconButton icon={X} label={m.policy_map_remove()} onclick={() => (form.map = form.map.filter((r) => r !== rule))} />
					</div>
				{/each}
				<div><Button variant="ghost" icon={Plus} onclick={() => (form.map = [...form.map, { source: '', target: '' }])} data-testid="policy-map-add">{m.policy_map_add()}</Button></div>
			</fieldset>

			<Field label={m.policy_exclude()} hint={m.policy_exclude_hint()}>
				<textarea class="input font-mono" rows="2" bind:value={form.exclude} data-testid="policy-exclude"></textarea>
			</Field>
			<div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
				<Field label={m.policy_date_from()}><input class="input" type="date" bind:value={form.dateFrom} data-testid="policy-date-from" /></Field>
				<Field label={m.policy_date_to()}><input class="input" type="date" bind:value={form.dateTo} data-testid="policy-date-to" /></Field>
				<Field label={m.policy_max_size()}><input class="input font-mono" inputmode="decimal" bind:value={form.maxSize} data-testid="policy-max-size" /></Field>
			</div>

			{#if server}
				<fieldset class="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
					<legend class="mb-1 p-0 font-medium text-ink-2">{m.policy_hours()}</legend>
					<p class="m-0 text-xs text-ink-3">{m.policy_hours_hint()}</p>
					<div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
						<Field label={m.policy_start()}><input class="input" type="time" bind:value={form.start} data-testid="policy-start" /></Field>
						<Field label={m.policy_end()}><input class="input" type="time" bind:value={form.end} data-testid="policy-end" /></Field>
						<Field label={m.policy_tz()}><input class="input font-mono" placeholder="Europe/Paris" autocomplete="off" bind:value={form.tz} data-testid="policy-tz" /></Field>
					</div>
					<div class="flex flex-wrap gap-1" role="group" aria-label={m.policy_days()}>
						{#each [0, 1, 2, 3, 4, 5, 6] as day (day)}
							<button
								type="button"
								class={`inline-flex min-h-10 min-w-10 items-center justify-center rounded-btn border px-2 text-xs ${form.days.includes(day) ? 'border-line-strong bg-surface-2 font-semibold text-ink' : 'border-dashed border-line text-ink-3'}`}
								aria-pressed={form.days.includes(day)}
								onclick={() => toggleDay(day)}
							>
								{fmtWeekday(day)}
							</button>
						{/each}
					</div>
					<small class="text-xs text-ink-3">{m.policy_days_hint()}</small>
				</fieldset>
				<Field label={m.policy_rate()} hint={m.policy_rate_hint()}>
					<input class="input max-w-40 font-mono" inputmode="decimal" bind:value={form.rate} data-testid="policy-rate" />
				</Field>
			{/if}
		</div>
	{/if}
</Disclosure>
