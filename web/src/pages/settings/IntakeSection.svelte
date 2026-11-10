<script lang="ts">
	/**
	 * Drop folder and automatic assignment. Every control saves at once: a toggle answers on the spot and goes
	 * back with a message if the server refuses.
	 */
	import Settings from '@lucide/svelte/icons/settings';
	import { onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText } from '../../lib/api/client.ts';
	import type { IntakeSettings, IntakeSettingsPatch } from '../../lib/api/types.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import { intake } from '../../lib/stores/intake.svelte.ts';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Field from '../../lib/ui/Field.svelte';
	import Help from '../../lib/ui/Help.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import SettingRow from '../../lib/ui/SettingRow.svelte';
	import Skeleton from '../../lib/ui/Skeleton.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';

	onMount(() => {
		if (!intake.settings) void intake.load();
	});

	const settings = $derived(intake.settings);
	let editing = $state(false);
	const chainState = $derived(
		!settings?.auto_chain.enabled ? m.settings_state_off() : settings.auto_chain.start_import ? m.settings_intake_chain_state_start() : m.settings_state_on()
	);

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

<div data-testid="section-intake-body">
	{#if settings}
		<div class="divide-y divide-line">
			<SettingRow label={m.settings_intake_inbox()}>
				<span class="check"><input class="switch" type="checkbox" checked={settings.inbox.enabled} onchange={(e) => change({ inbox: { enabled: e.currentTarget.checked } }, { inbox: { ...settings.inbox, enabled: e.currentTarget.checked } })} /></span>
			</SettingRow>
			<SettingRow as="div" label={m.settings_intake_folder()}>
				<code class="min-w-0 truncate rounded-plate bg-surface-2 px-2 py-1 font-mono text-xs" data-testid="intake-path">{settings.inbox.path}</code>
				<CopyButton text={settings.inbox.path} label={m.settings_intake_copy()} />
			</SettingRow>
			<SettingRow as="div" label={m.settings_intake_chain()}>
				<span class="text-ink-2" data-testid="intake-chain-state">{chainState}</span>
				<IconButton icon={Settings} label={m.settings_intake_chain_configure()} onclick={() => (editing = true)} data-testid="intake-chain-open" />
			</SettingRow>
		</div>
	{:else if intake.error}
		<div class="p-4"><ErrorNotice title={m.settings_intake_load_failed()} message={intake.error} onretry={() => intake.load()} /></div>
	{:else}
		<div class="p-4"><Skeleton class="h-24" /></div>
	{/if}

	{#if editing && settings}
		<Dialog title={m.settings_intake_chain()} onclose={() => (editing = false)}>
			<div class="flex flex-col gap-5">
				<div class="divide-y divide-line rounded-card border border-line-strong">
					<SettingRow label={m.settings_intake_chain()} help={m.settings_intake_chain_hint()}>
						<span class="check"><input class="switch" type="checkbox" checked={settings.auto_chain.enabled} onchange={(e) => change({ auto_chain: { enabled: e.currentTarget.checked } }, { auto_chain: { ...settings.auto_chain, enabled: e.currentTarget.checked } })} /></span>
					</SettingRow>
					{#if settings.auto_chain.enabled}
						<SettingRow label={m.settings_intake_start()} help={m.settings_intake_start_hint()} data-testid="intake-chain-options">
							<span class="check"><input class="switch" type="checkbox" checked={settings.auto_chain.start_import} onchange={(e) => change({ auto_chain: { start_import: e.currentTarget.checked } }, { auto_chain: { ...settings.auto_chain, start_import: e.currentTarget.checked } })} /></span>
						</SettingRow>
					{/if}
				</div>
				{#if settings.auto_chain.enabled && data.servers.length}
					<div class="flex flex-col gap-3" role="group" aria-labelledby="intake-domains-title" data-testid="intake-domains">
						<div class="relative flex items-center gap-1">
							<h3 id="intake-domains-title" class="text-sm font-semibold">{m.settings_intake_domains()}</h3>
							<Help text={m.settings_intake_domains_hint()} />
						</div>
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
				{/if}
			</div>
		</Dialog>
	{/if}
</div>
