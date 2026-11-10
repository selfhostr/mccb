<script lang="ts">
	/**
	 * The rows of the saved connections in the list of servers. A Microsoft 365 tenant is tested and
	 * migrated from its matching screen; any other kind is a server its mailboxes share.
	 */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import CloudOff from '@lucide/svelte/icons/cloud-off';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Shuffle from '@lucide/svelte/icons/arrow-right-left';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText } from '../../lib/api/client.ts';
	import type { CheckResult, SourceConnection } from '../../lib/api/types.ts';
	import { kindLabel } from '../../lib/logic/connector.ts';
	import { kindIcon, kindMark } from '../../lib/logic/providers.ts';
	import { connectionAddress, isDirectory } from '../../lib/logic/serverKinds.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import CheckList from './CheckList.svelte';
	import ProvisionPanel from './ProvisionPanel.svelte';
	import SourceDialog from './SourceDialog.svelte';

	void sources.load();

	let editing = $state.raw<SourceConnection | null>(null);
	const directory = (c: SourceConnection) => isDirectory(connectors.list, c.kind);

	/** After a save: a tenant is tested at once, the other kinds are checked with each mailbox. */
	export function check(c: SourceConnection) {
		if (directory(c)) void test(c);
	}
	let unprovisioning = $state.raw<SourceConnection | null>(null);
	let removing = $state.raw<SourceConnection | null>(null);
	const results = new SvelteMap<number, CheckResult>();
	const testing = new SvelteSet<number>();

	function verdict(c: SourceConnection) {
		if (testing.has(c.id)) return { tone: 'info' as const, icon: FlaskConical, label: m.migrations_test_running() };
		const result = results.get(c.id) ?? c.last_test_result;
		if (!result) return { tone: 'ghost' as const, icon: CircleDashed, label: m.sources_verdict_never() };
		return result.ok ? { tone: 'signal' as const, icon: CircleCheck, label: m.sources_verdict_ok() } : { tone: 'warning' as const, icon: TriangleAlert, label: m.sources_verdict_failed() };
	}

	async function test(c: SourceConnection) {
		if (testing.has(c.id)) return;
		testing.add(c.id);
		try {
			results.set(c.id, await sources.testStored(c.id));
		} catch (error) {
			toast(errorText(error), 'warning');
		} finally {
			testing.delete(c.id);
		}
	}

	async function remove(c: SourceConnection) {
		try {
			await sources.remove(c.id);
			results.delete(c.id);
			toast(m.sources_deleted({ name: c.name }), 'signal');
		} catch (error) {
			const detail = error instanceof ApiError && error.status === 409 ? (error.body as { detail?: { accounts?: unknown } } | null)?.detail : null;
			const linked = typeof detail?.accounts === 'number' ? detail.accounts : null;
			toast(linked == null ? errorText(error) : directory(c) ? m.sources_delete_linked({ count: linked }) : m.servers_delete_linked({ count: linked }), 'warning');
		}
	}
</script>

{#if sources.list.length}
	<ul class="m-0 flex list-none flex-col divide-y divide-line border-t border-line p-0 first:border-t-0" data-testid="source-list">
		{#each sources.list as c (c.id)}
			{@const tenant = directory(c)}
			{@const look = verdict(c)}
			{@const result = results.get(c.id) ?? (c.last_test_result && !c.last_test_result.ok ? c.last_test_result : undefined)}
			<li class="py-2 pr-2 pl-4" data-source={c.name} data-kind={c.kind}>
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
					<KindIcon icon={kindIcon(c.kind)} mark={kindMark(c.kind)} />
					<div class="min-w-0 flex-1 basis-56">
						<p class="m-0 flex flex-wrap items-center gap-2 font-medium">
							<span>{c.name}</span>
							{#if tenant}<span data-testid="source-verdict"><Plate tone={look.tone} icon={look.icon} label={look.label} /></span>{/if}
						</p>
						<p class="m-0 font-mono text-xs break-all text-ink-2">{connectionAddress(c.config)}</p>
						<p class="m-0 flex flex-wrap gap-x-3 text-xs text-ink-3">
							<span>{kindLabel(connectors.list, c.kind)}</span>
							<span>{m.sources_accounts({ count: c.accounts })}</span>
						</p>
					</div>
					<div class="flex shrink-0 items-center">
						{#if tenant}<IconButton icon={FlaskConical} label={m.sources_test({ name: c.name })} loading={testing.has(c.id)} onclick={() => test(c)} />{/if}
						<Menu
							label={m.sources_more({ name: c.name })}
							icon={EllipsisVertical}
							items={[
								{ label: m.sources_edit(), icon: Pencil, onselect: () => (editing = c) },
								...(tenant ? [{ label: m.sources_migrate(), icon: Shuffle, onselect: () => router.go(`/tenants/${c.id}/migrate`) }] : []),
								...(c.provisioned ? [{ label: m.provision_remove_menu(), icon: CloudOff, onselect: () => (unprovisioning = c) }] : []),
								{ label: m.sources_delete(), icon: Trash2, danger: true, onselect: () => (removing = c) }
							]}
						/>
					</div>
				</div>
				{#if tenant && result}<div class="mt-1"><CheckList {result} /></div>{/if}
			</li>
		{/each}
	</ul>
{/if}

{#if editing}
	<SourceDialog connection={editing} onclose={() => (editing = null)} onsaved={check} />
{/if}
{#if unprovisioning}
	{@const target = unprovisioning}
	<Dialog title={m.provision_remove_title({ name: target.name })} onclose={() => (unprovisioning = null)}>
		<p class="m-0 mb-3 text-sm text-ink-2">{m.provision_remove_text()}</p>
		<ProvisionPanel remove start={() => sources.deprovision(target.id)} oncancel={() => (unprovisioning = null)} onclose={() => (unprovisioning = null)} />
	</Dialog>
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={directory(target) ? m.sources_delete_title() : m.settings_server_delete_title()} text={m.sources_delete_text({ name: target.name })} confirm={m.sources_delete()} danger onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
