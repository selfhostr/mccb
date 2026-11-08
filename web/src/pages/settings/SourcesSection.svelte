<script lang="ts">
	/** Microsoft 365 tenants: registered once, tested, then migrated from the matching screen. */
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import Cloud from '@lucide/svelte/icons/cloud';
	import EllipsisVertical from '@lucide/svelte/icons/ellipsis-vertical';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import Pencil from '@lucide/svelte/icons/pencil';
	import Plus from '@lucide/svelte/icons/plus';
	import Shuffle from '@lucide/svelte/icons/arrow-right-left';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText } from '../../lib/api/client.ts';
	import type { CheckResult, SourceConnection } from '../../lib/api/types.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Confirm from '../../lib/ui/Confirm.svelte';
	import EmptyState from '../../lib/ui/EmptyState.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import Menu from '../../lib/ui/Menu.svelte';
	import Panel from '../../lib/ui/Panel.svelte';
	import Plate from '../../lib/ui/Plate.svelte';
	import Section from '../../lib/ui/Section.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import CheckList from './CheckList.svelte';
	import SourceDialog from './SourceDialog.svelte';

	void sources.load();

	let editing = $state.raw<SourceConnection | 'new' | null>(null);
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
			toast(typeof detail?.accounts === 'number' ? m.sources_delete_linked({ count: detail.accounts }) : errorText(error), 'warning');
		}
	}
</script>

<Section id="tenants" title={m.sources_title()} sentence={m.sources_sentence()} bare>
	{#snippet actions()}
		<Button icon={Plus} onclick={() => (editing = 'new')} data-testid="source-add">{m.sources_add()}</Button>
	{/snippet}

	{#if sources.loaded && !sources.list.length}
		<EmptyState icon={Cloud} title={m.sources_empty_title()} description={m.sources_empty_text()} />
	{:else if sources.list.length}
		<Panel>
			<ul class="m-0 flex list-none flex-col divide-y divide-line p-0" data-testid="source-list">
				{#each sources.list as c (c.id)}
					{@const look = verdict(c)}
					{@const result = results.get(c.id) ?? (c.last_test_result && !c.last_test_result.ok ? c.last_test_result : undefined)}
					<li class="py-2 pr-2 pl-4" data-source={c.name}>
						<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
							<KindIcon icon={Cloud} />
							<div class="min-w-0 flex-1 basis-56">
								<p class="m-0 flex flex-wrap items-center gap-2 font-medium">
									<span>{c.name}</span>
									<span data-testid="source-verdict"><Plate tone={look.tone} icon={look.icon} label={look.label} /></span>
								</p>
								<p class="m-0 font-mono text-xs text-ink-2">{String(c.config.tenant_id ?? '')}</p>
								<p class="m-0 text-xs text-ink-3">{m.sources_accounts({ count: c.accounts })}</p>
							</div>
							<div class="flex shrink-0 items-center">
								<IconButton icon={FlaskConical} label={m.sources_test({ name: c.name })} loading={testing.has(c.id)} onclick={() => test(c)} />
								<Menu
									label={m.sources_more({ name: c.name })}
									icon={EllipsisVertical}
									items={[
										{ label: m.sources_edit(), icon: Pencil, onselect: () => (editing = c) },
										{ label: m.sources_migrate(), icon: Shuffle, onselect: () => router.go(`/tenants/${c.id}/migrate`) },
										{ label: m.sources_delete(), icon: Trash2, danger: true, onselect: () => (removing = c) }
									]}
								/>
							</div>
						</div>
						{#if result}<div class="mt-1"><CheckList {result} /></div>{/if}
					</li>
				{/each}
			</ul>
		</Panel>
	{/if}
</Section>

{#if editing}
	<SourceDialog connection={editing === 'new' ? null : editing} onclose={() => (editing = null)} onsaved={(saved) => test(saved)} />
{/if}
{#if removing}
	{@const target = removing}
	<Confirm title={m.sources_delete_title()} text={m.sources_delete_text({ name: target.name })} confirm={m.sources_delete()} danger onconfirm={() => remove(target)} onclose={() => (removing = null)} />
{/if}
