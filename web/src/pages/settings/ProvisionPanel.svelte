<script lang="ts">
	/**
	 * The "Connect with Microsoft" flow (or its removal): the device code to type, the live steps, the end.
	 * It starts the flow itself, polls until a final state, and cancels the flow if it goes away while it waits.
	 */
	import Check from '@lucide/svelte/icons/check';
	import CircleDashed from '@lucide/svelte/icons/circle-dashed';
	import FlaskConical from '@lucide/svelte/icons/flask-conical';
	import ExternalLink from '@lucide/svelte/icons/external-link';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Shuffle from '@lucide/svelte/icons/arrow-right-left';
	import X from '@lucide/svelte/icons/x';
	import { onDestroy, onMount } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { ApiError, errorText } from '../../lib/api/client.ts';
	import type { ProvisionFlow, ProvisionState, ProvisionStep } from '../../lib/api/types.ts';
	import { router } from '../../lib/router.svelte.ts';
	import { sources } from '../../lib/stores/sources.svelte.ts';
	import Button from '../../lib/ui/Button.svelte';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import Notice from '../../lib/ui/Notice.svelte';
	import CheckList from './CheckList.svelte';

	interface Props {
		/** Starts the flow: provision or deprovision. Called again on Retry. */
		start: () => Promise<ProvisionFlow>;
		remove?: boolean;
		/** Cancel pressed: the flow is cancelled, the parent decides where to go. */
		oncancel: () => void;
		/** Error fallback: the parent shows the manual form. */
		onmanual?: () => void;
		onclose: () => void;
	}
	let { start, remove = false, oncancel, onmanual, onclose }: Props = $props();

	const STEPS: ProvisionStep[] = $derived(remove ? ['signin', 'remove'] : ['signin', 'app', 'consent', 'secret', 'propagation', 'test']);
	const WORDS: Record<ProvisionStep, () => string> = {
		signin: () => m.provision_step_signin(),
		app: () => m.provision_step_app(),
		consent: () => m.provision_step_consent(),
		secret: () => m.provision_step_secret(),
		propagation: () => m.provision_step_propagation(),
		test: () => m.provision_step_test(),
		remove: () => m.provision_step_remove()
	};

	let flow = $state.raw<ProvisionFlow | null>(null);
	let current = $state.raw<ProvisionState>({ status: 'waiting', step: 'signin', error: null, connection: null });
	let startError = $state<string | null>(null);
	let run = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let active: string | null = null;

	const finished = $derived(current.status === 'done');
	// Done with an error: the app exists but the permissions are not active yet. Retry would create a second app.
	const pending = $derived(finished && !remove && !!current.error);
	let testing = $state(false);
	const failed = $derived(!!startError || current.status === 'error' || current.status === 'expired');
	const message = $derived(startError ?? current.error ?? (current.status === 'expired' ? m.provision_expired() : ''));
	const stepIndex = $derived(current.step ? STEPS.indexOf(current.step) : 0);

	function stepState(i: number): 'done' | 'current' | 'failed' | 'pending' {
		if (finished) return 'done';
		if (i < stepIndex) return 'done';
		if (i === stepIndex) return failed ? 'failed' : 'current';
		return 'pending';
	}

	async function begin() {
		const mine = ++run;
		clearTimeout(timer);
		flow = null;
		startError = null;
		current = { status: 'waiting', step: 'signin', error: null, connection: null };
		let started: ProvisionFlow;
		try {
			started = await start();
		} catch (error) {
			if (mine === run) startError = errorText(error);
			return;
		}
		if (mine !== run) {
			void sources.cancelFlow(started.flow_id).catch(() => {});
			return;
		}
		flow = started;
		active = started.flow_id;
		poll(mine, started);
	}

	function poll(mine: number, f: ProvisionFlow) {
		timer = setTimeout(async () => {
			if (mine !== run) return;
			try {
				const next = await sources.flowStatus(f.flow_id);
				if (mine !== run) return;
				current = next;
				if (next.status === 'done' || next.status === 'error' || next.status === 'expired') {
					active = null;
					if (next.status === 'done') void sources.reload();
					return;
				}
			} catch (error) {
				if (mine !== run) return;
				// The flow is gone (cancelled, or older than the server keeps it): nothing more to say.
				if (error instanceof ApiError && error.status === 404) {
					active = null;
					return;
				}
				startError = errorText(error);
				active = null;
				return;
			}
			poll(mine, f);
		}, Math.max(2, f.interval || 2) * 1000);
	}

	async function testAgain() {
		const c = current.connection;
		if (!c) return;
		testing = true;
		try {
			const result = await sources.testStored(c.id);
			current = { ...current, connection: { ...c, last_test_result: result }, error: result.ok ? null : current.error };
		} catch (error) {
			startError = errorText(error);
		} finally {
			testing = false;
		}
	}

	function stop() {
		run++;
		clearTimeout(timer);
		const id = active;
		active = null;
		if (id) void sources.cancelFlow(id).catch(() => {});
	}

	function cancel() {
		stop();
		oncancel();
	}

	onMount(() => void begin());
	onDestroy(stop);
</script>

<div class="flex flex-col gap-3" data-testid="provision-panel" data-status={startError ? 'error' : current.status}>
	{#if failed}
		<ErrorNotice title={m.provision_failed()} message={message} />
		<div class="flex flex-wrap gap-2">
			<Button onclick={begin} data-testid="provision-retry">{m.common_retry()}</Button>
			{#if onmanual}<Button variant="ghost" onclick={onmanual} data-testid="provision-manual">{m.provision_manual_instead()}</Button>{/if}
			<Button variant="ghost" onclick={onclose}>{m.common_close()}</Button>
		</div>
	{:else if finished}
		{#if pending}
			<Notice><p class="m-0" data-testid="provision-pending">{m.provision_pending()} {current.error}</p></Notice>
		{:else}
			<p class="m-0 font-medium" data-testid="provision-done">{remove ? m.provision_removed() : m.provision_done()}</p>
		{/if}
		{#if current.connection?.last_test_result && !remove}<CheckList result={current.connection.last_test_result} />{/if}
		<div class="flex flex-wrap justify-end gap-2">
			{#if pending}<Button icon={FlaskConical} loading={testing} onclick={testAgain} data-testid="provision-test-again">{m.provision_test_again()}</Button>{/if}
			{#if !remove && current.connection}
				{@const id = current.connection.id}
				<Button variant="primary" icon={Shuffle} onclick={() => { onclose(); router.go(`/tenants/${id}/migrate`); }} data-testid="provision-migrate">{m.provision_migrate()}</Button>
			{/if}
			<Button variant={remove ? 'primary' : 'ghost'} onclick={onclose} data-testid="provision-close">{m.common_close()}</Button>
		</div>
	{:else}
		{#if flow}
			<p class="m-0 text-sm">{m.provision_instruction()}</p>
			<p class="m-0 text-xs text-ink-3" data-testid="provision-notice">{m.provision_notice()}</p>
			<div class="flex flex-wrap items-center gap-2 rounded-card border border-line bg-surface-2 p-3">
				<div class="min-w-0 flex-1">
					<p class="m-0 text-xs text-ink-3">{m.provision_code()}</p>
					<p class="m-0 font-mono text-3xl font-semibold tracking-widest select-all" data-testid="provision-code">{flow.user_code}</p>
				</div>
				<CopyButton text={flow.user_code} label={m.provision_copy_code()} />
				<Button variant="primary" icon={ExternalLink} onclick={() => window.open(flow?.verification_uri, '_blank', 'noopener,noreferrer')} data-testid="provision-open">{m.provision_open()}</Button>
			</div>
		{:else}
			<p class="m-0 flex items-center gap-2 text-sm text-ink-2"><LoaderCircle size={14} class="animate-spin" />{m.provision_starting()}</p>
		{/if}
		<ol class="m-0 flex list-none flex-col gap-1.5 p-0" data-testid="provision-steps">
			{#each STEPS as step, i (step)}
				{@const st = stepState(i)}
				<li class={`flex items-start gap-2 text-sm ${st === 'pending' ? 'text-ink-3' : 'text-ink'}`} data-step={step} data-state={st}>
					<span class="mt-0.5 shrink-0">
						{#if st === 'done'}<Check size={14} class="text-signal-ink" />
						{:else if st === 'current'}<LoaderCircle size={14} class="animate-spin" />
						{:else}<CircleDashed size={14} />{/if}
					</span>
					<span>{WORDS[step]()}</span>
				</li>
			{/each}
		</ol>
		<div class="flex justify-end">
			<Button variant="ghost" icon={X} onclick={cancel} data-testid="provision-cancel">{m.common_cancel()}</Button>
		</div>
	{/if}
</div>
