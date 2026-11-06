<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Plus from '@lucide/svelte/icons/plus';
	import ServerIcon from '@lucide/svelte/icons/server';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import X from '@lucide/svelte/icons/x';
	import { onMount } from 'svelte';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { del, errorText, get, post } from '../../lib/api/client.ts';
	import type { Connector, Server, TestResult } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import ServerDialog from './ServerDialog.svelte';

	const PROTOCOLS = [
		{ key: 'imap', name: 'IMAP' },
		{ key: 'caldav', name: 'CalDAV' },
		{ key: 'carddav', name: 'CardDAV' }
	] as const;

	let editing = $state.raw<Server | 'new' | null>(null);
	let removing = $state.raw<Server | null>(null);
	/** Server tests are not stored by the server: these last until the page closes, and the page says so. */
	const results = new SvelteMap<number, TestResult>();
	const testing = new SvelteSet<number>();
	let connectors = $state.raw<Connector[]>([]);

	function securityLabel(value: Server['imap_security']) {
		return value === 'ssl' ? m.settings_server_security_ssl() : value === 'starttls' ? m.settings_server_security_starttls() : m.settings_server_security_none();
	}

	async function test(server: Server) {
		if (testing.has(server.id)) return;
		testing.add(server.id);
		try {
			results.set(server.id, await post<TestResult>(`/servers/${server.id}/test`));
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

	onMount(async () => {
		// Catalogue of another branch: shown as served, and simply absent if the route is not there yet.
		try {
			const list = await get<Connector[] | { connectors?: Connector[] }>('/connectors');
			connectors = Array.isArray(list) ? list : (list.connectors ?? []);
		} catch {
			connectors = [];
		}
	});
</script>

<section id="servers" class="scroll-mt-[calc(var(--bar-h)+12px)]" data-testid="section-servers">
	<div class="mb-2 flex flex-wrap items-center justify-between gap-2">
		<div>
			<h2 class="text-[0.9375rem]">{m.settings_servers_title()}</h2>
			<p class="m-0 text-ink-2">{m.settings_servers_sentence()}</p>
		</div>
		<Button icon={Plus} onclick={() => (editing = 'new')}>{m.settings_servers_add()}</Button>
	</div>

	{#if !data.loading && !data.servers.length}
		<EmptyState icon={ServerIcon} title={m.settings_servers_empty_title()} description={m.settings_servers_empty_text()} />
	{:else}
		<ul class="m-0 flex list-none flex-col p-0" data-testid="server-list">
			{#each data.servers as server (server.id)}
				{@const result = results.get(server.id)}
				<li class="border-b border-line py-2" data-server={server.name}>
					<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
						<div class="min-w-0 flex-1 basis-56">
							<p class="m-0 font-medium">{server.name}</p>
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
						<p class="m-0 mt-1 text-xs text-ink-3">{m.settings_server_test_not_saved()}</p>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

	{#if connectors.length}
		<div class="mt-3" data-testid="connectors">
			<h3 class="text-sm">{m.settings_connectors_title()}</h3>
			<ul class="m-0 mt-1 flex list-none flex-col gap-1 p-0">
				{#each connectors as connector, i (connector.id ?? connector.kind ?? i)}
					<li>
						<span class="font-medium">{connector.name ?? connector.label ?? connector.id ?? connector.kind}</span>
						{#if connector.description}<span class="block text-xs text-ink-3">{connector.description}</span>{/if}
					</li>
				{/each}
			</ul>
		</div>
	{/if}
</section>

{#if editing}
	<ServerDialog server={editing === 'new' ? null : editing} onclose={() => (editing = null)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.settings_server_delete_title()} text={m.settings_server_delete_text({ name: target.name })} confirm={m.settings_server_delete()} danger onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
