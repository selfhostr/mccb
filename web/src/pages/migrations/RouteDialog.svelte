<script lang="ts">
	/**
	 * Where an account's items come from and where they go. The source is the account's PST unless another
	 * connector is chosen; the target is the account's server unless the account has its own. Both are quiet:
	 * the target sits behind a disclosure.
	 */
	import { untrack } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, put } from '../../lib/api/client.ts';
	import type { Account, EndpointInput } from '../../lib/api/types.ts';
	import { kindLabel } from '../../lib/logic/connector.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import { data } from '../../lib/stores/data.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import Disclosure from '../../lib/ui/Disclosure.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import { toast } from '../../lib/ui/toast.svelte.ts';
	import EndpointEditor from './EndpointEditor.svelte';

	let { account, onclose }: { account: Account; onclose: () => void } = $props();

	let source = $state<EndpointEditor>();
	let target = $state<EndpointEditor>();
	let busy = $state(false);
	let error = $state<string | null>(null);
	let targetTouched = $state(false);
	let targetOpen = $state(untrack(() => !!account.target?.kind));

	const targetName = $derived(account.target?.kind ? kindLabel(connectors.list, account.target.kind) : m.migrations_route_target_server());

	async function save(e: SubmitEvent) {
		e.preventDefault();
		const plan: { role: 'source' | 'target'; input: EndpointInput }[] = [];
		error = null;
		for (const [role, editor] of [['source', source], ['target', target]] as const) {
			const result = editor?.body();
			if (!result || 'skip' in result) continue;
			if ('problem' in result) {
				// The message sits at the field; a target problem must not stay behind a closed disclosure.
				if (role === 'target') targetOpen = true;
				return;
			}
			plan.push({ role, input: result.input });
		}
		if (!plan.length) return onclose();
		busy = true;
		error = null;
		try {
			// Source first: a refusal there leaves the target as it was.
			for (const { role, input } of plan) data.upsertAccount(await put<Account>(`/accounts/${account.id}/${role}`, input));
			toast(m.migrations_route_saved(), 'signal');
			onclose();
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Dialog title={m.migrations_route_title({ email: account.email })} {onclose} wide>
	<form id="route-form" class="flex flex-col gap-3" onsubmit={save}>
		<EndpointEditor bind:this={source} role="source" {account} />
		<Disclosure title={m.migrations_route_target()} summary={targetName} bind:open={targetOpen} data-testid="route-target-disclosure">
			<div class="flex flex-col gap-3">
				<EndpointEditor bind:this={target} role="target" {account} onedit={() => (targetTouched = true)} />
				{#if targetTouched && target?.changed()}
					<Notice><p class="m-0" data-testid="route-resend">{m.migrations_route_resend()}</p></Notice>
				{/if}
			</div>
		</Disclosure>
		{#if error}<ErrorNotice title={m.migrations_route_save_failed()} message={error} />{/if}
	</form>
	{#snippet actions()}
		<Button variant="ghost" onclick={onclose}>{m.common_cancel()}</Button>
		<Button variant="primary" type="submit" form="route-form" loading={busy} data-testid="route-save">{m.common_save()}</Button>
	{/snippet}
</Dialog>
