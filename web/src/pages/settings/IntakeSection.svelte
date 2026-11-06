<script lang="ts">
	/**
	 * Drop folder and automatic assignment. Every control saves at once: a toggle answers on the spot and goes
	 * back with a message if the server refuses.
	 */
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import type { IntakeSettings, IntakeSettingsPatch } from '../../lib/api/types.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import Toggle from '../../lib/ui/Toggle.svelte';

	onMount(() => {
		if (!intake.settings) void intake.load();
	});

	const settings = $derived(intake.settings);

	async function change(patch: IntakeSettingsPatch, shown: Partial<IntakeSettings>): Promise<boolean> {
		const before = intake.settings;
		if (!before) return false;
		intake.settings = { ...before, ...shown };
		try {
			await intake.save(patch);
			return true;
		} catch (error) {
			intake.settings = before;
			toast(errorText(error), 'warning');
			return false;
		}
	}

	async function setDomain(serverId: number, input: HTMLInputElement) {
		const current = intake.settings?.auto_chain;
		if (!current) return;
		const next = { ...current.default_domains };
		const value = input.value.trim().replace(/^@/, '');
		if (value) next[String(serverId)] = value;
		else delete next[String(serverId)];
		const ok = await change({ auto_chain: { default_domains: next } }, { auto_chain: { ...current, default_domains: next } });
		if (!ok) input.value = current.default_domains[String(serverId)] ?? '';
	}
</script>

<section id="intake" class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid="section-intake">
	<h2 class="text-[0.9375rem]">{m.settings_intake_title()}</h2>
	<p class="m-0 mt-1 mb-2 text-ink-2">{m.settings_intake_sentence()}</p>
	{#if settings}
		<div class="flex flex-col gap-1">
			<Toggle
				checked={settings.inbox.enabled}
				label={m.settings_intake_inbox()}
				hint={m.settings_intake_inbox_hint()}
				onchange={(v) => change({ inbox: { enabled: v } }, { inbox: { ...settings.inbox, enabled: v } })}
			/>
			<div class="flex items-center gap-1 pl-10">
				<code class="min-w-0 truncate rounded-plate bg-surface-2 px-2 py-1 font-mono text-xs" data-testid="intake-path">{settings.inbox.path}</code>
				<CopyButton text={settings.inbox.path} label={m.settings_intake_copy()} />
			</div>
			<Toggle
				checked={settings.auto_chain.enabled}
				label={m.settings_intake_chain()}
				hint={m.settings_intake_chain_hint()}
				onchange={(v) => change({ auto_chain: { enabled: v } }, { auto_chain: { ...settings.auto_chain, enabled: v } })}
			/>
			{#if settings.auto_chain.enabled}
				<div class="pl-10" data-testid="intake-chain-options">
					<Toggle
						checked={settings.auto_chain.start_import}
						label={m.settings_intake_start()}
						hint={m.settings_intake_start_hint()}
						onchange={(v) => change({ auto_chain: { start_import: v } }, { auto_chain: { ...settings.auto_chain, start_import: v } })}
					/>
					{#if data.servers.length}
						<Disclosure title={m.settings_intake_domains()} summary={m.settings_intake_domains_set({ count: Object.keys(settings.auto_chain.default_domains).length })}>
							<div class="flex flex-col gap-3">
								<p class="m-0 text-ink-2">{m.settings_intake_domains_hint()}</p>
								<div class="grid max-w-[560px] grid-cols-1 gap-3 sm:grid-cols-2">
									{#each data.servers as server (server.id)}
										<Field label={server.name}>
											<input
												class="input font-mono"
												autocomplete="off"
												placeholder="example.com"
												value={settings.auto_chain.default_domains[String(server.id)] ?? ''}
												onchange={(e) => setDomain(server.id, e.currentTarget)}
												data-testid={`domain-${server.id}`}
											/>
										</Field>
									{/each}
								</div>
							</div>
						</Disclosure>
					{/if}
				</div>
			{/if}
		</div>
	{:else if intake.error}
		<ErrorNotice title={m.settings_intake_load_failed()} message={intake.error} onretry={() => intake.load()} />
	{:else}
		<Skeleton class="h-24" />
	{/if}
</section>
