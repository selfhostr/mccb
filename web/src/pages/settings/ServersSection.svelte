<script lang="ts">
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import { providerOf } from '../../lib/logic/providers.ts';
	import Check from '@lucide/svelte/icons/check';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Plus from '@lucide/svelte/icons/plus';
	import ServerIcon from '@lucide/svelte/icons/server';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import X from '@lucide/svelte/icons/x';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { del, errorText, post } from '../../lib/api/client.ts';
	import type { Server, TestResult } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ServerDialog from './ServerDialog.svelte';

	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;

	let editing = $state.raw<Server | 'new' | null>(null);
	let removing = $state.raw<Server | null>(null);
	/** Tests run from this page: their details stay open until the page closes. The verdict itself is stored. */
	const results = new SvelteMap<number, TestResult>();
	const testing = new SvelteSet<number>();

	const passed = (result: TestResult) => [result.imap, result.caldav, result.carddav].every((r) => !r || r.ok);

	/** The verdict next to the name: verified, failed, never checked, or checking now. */
	function verdict(server: Server) {
		if (testing.has(server.id)) return { tone: 'info' as const, icon: FlaskConical, label: m.migrations_test_running() };
		const result = results.get(server.id) ?? server.last_test_result;
		if (!result) return { tone: 'ghost' as const, icon: CircleDashed, label: m.settings_server_check_never() };
		return passed(result)
			? { tone: 'signal' as const, icon: CircleCheck, label: m.settings_server_check_ok() }
			: { tone: 'warning' as const, icon: TriangleAlert, label: m.settings_server_check_failed() };
	}

	function securityLabel(value: Server['imap_security']) {
		return value === 'ssl' ? m.settings_server_security_ssl() : value === 'starttls' ? m.settings_server_security_starttls() : m.settings_server_security_none();
	}

	async function test(server: Server) {
		if (testing.has(server.id)) return;
		testing.add(server.id);
		try {
			results.set(server.id, await post<TestResult>(`/servers/${server.id}/test`));
			await data.reloadServers();
		} catch (error) {
			toast(errorText(error), 'warning');
		} finally {
			testing.delete(server.id);
		}
	}

	async function remove(server: Server) {
		try {
			await del(`/servers/${server.id}`);
			await data.load();
			toast(m.settings_server_deleted({ name: server.name }), 'signal');
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}
</script>

<Section id="servers" title={m.settings_servers_title()} sentence={m.settings_servers_sentence()} bare>
	{#snippet actions()}
		<Button icon={Plus} onclick={() => (editing = 'new')}>{m.settings_servers_add()}</Button>
	{/snippet}

	{#if !data.loading && !data.servers.length}
		<EmptyState icon={ServerIcon} title={m.settings_servers_empty_title()} description={m.settings_servers_empty_text()} />
	{:else}
		<Panel>
			<ul class="m-0 flex list-none flex-col divide-y divide-line p-0" data-testid="server-list">
				{#each data.servers as server (server.id)}
					{@const stored = server.last_test_result}
					{@const result = results.get(server.id) ?? (stored && !passed(stored) ? stored : undefined)}
					{@const look = verdict(server)}
					{@const brand = providerOf(server.imap_host)}
					<li class="py-2 pr-2 pl-4" data-server={server.name}>
						<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
							<KindIcon icon={brand.icon} />
							<div class="min-w-0 flex-1 basis-56">
								<p class="m-0 flex flex-wrap items-center gap-2 font-medium">
									<span>{server.name}</span>
									<span data-testid="server-verdict"><Plate tone={look.tone} icon={look.icon} label={look.label} /></span>
								</p>
								<p class="m-0 font-mono text-xs text-ink-2">
									{prefs.host(server.imap_host)}:{server.imap_port}
								</p>
								<p class="m-0 text-xs text-ink-3">
									{securityLabel(server.imap_security)}
									{server.dav_enabled ? m.settings_server_dav_on() : m.settings_server_dav_off()}
								</p>
							</div>
							<div class="flex shrink-0 items-center">
								<IconButton icon={FlaskConical} label={m.settings_server_test({ name: server.name })} loading={testing.has(server.id)} onclick={() => test(server)} />
								<Menu
									label={m.settings_server_more({ name: server.name })}
									icon={EllipsisVertical}
									items={[
										{ label: m.settings_server_edit(), icon: Pencil, onselect: () => (editing = server) },
										{ label: m.settings_server_delete(), icon: Trash2, danger: true, onselect: () => (removing = server) }
									]}
								/>
							</div>
						</div>
						{#if result}
							<ul class="m-0 mt-1 flex list-none flex-col gap-1 p-0" data-testid="server-test">
								{#each PROTOCOLS as protocol (protocol.key)}
									{@const probe = result[protocol.key]}
									{#if probe}
										<li class="flex items-start gap-2">
											<span class="w-24 shrink-0"><Plate tone={probe.ok ? 'signal' : 'warning'} icon={probe.ok ? Check : X} label={protocol.name} /></span>
											<span class="min-w-0 font-mono text-xs text-ink-2">{prefs.text(probe.msg)}</span>
										</li>
									{/if}
								{/each}
							</ul>
						{/if}
					</li>
				{/each}
			</ul>
		</Panel>
	{/if}

	{#if connectors.list.length}
		<Disclosure title={m.settings_connectors_title()} class="mt-2" data-testid="connectors">
			<ul class="m-0 flex list-none flex-col gap-1 p-0">
				{#each connectors.list as connector (connector.kind)}
					<li>
						<span class="font-medium">{connector.label || connector.kind}</span>
						{#if connector.description}<span class="block text-xs text-ink-3">{connector.description}</span>{/if}
					</li>
				{/each}
			</ul>
		</Disclosure>
	{/if}
</Section>

{#if editing}
	<ServerDialog server={editing === 'new' ? null : editing} onclose={() => (editing = null)} onsaved={(saved) => test(saved)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.settings_server_delete_title()} text={m.settings_server_delete_text({ name: target.name })} confirm={m.settings_server_delete()} danger onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
