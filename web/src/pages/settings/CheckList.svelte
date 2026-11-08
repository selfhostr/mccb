<script lang="ts">
	/** The checks of a tenant test: one plate per check, the missing permissions named in mono. */
	import Check from '@lucide/svelte/icons/check';
	import Minus from '@lucide/svelte/icons/minus';
	import X from '@lucide/svelte/icons/x';
	import { m } from '#lib/paraglide/messages.js';
	import type { CheckResult } from '../../lib/api/types.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Plate from '../../lib/ui/Plate.svelte';

	let { result }: { result: CheckResult } = $props();
</script>

<div data-testid="tenant-test">
	{#if result.tenant_name}<p class="m-0 mb-1 text-xs text-ink-2">{m.sources_check_tenant({ name: prefs.text(result.tenant_name) })}</p>{/if}
	<ul class="m-0 flex list-none flex-col gap-1 p-0">
		{#each result.checks as check (check.key)}
			<li class="flex items-start gap-2" data-check={check.key}>
				<span class="w-28 shrink-0">
					{#if check.ok}
						<Plate tone="signal" icon={Check} label={m.sources_check_ok()} />
					{:else if check.level === 'optional'}
						<Plate tone="advisory" icon={Minus} label={m.sources_check_optional()} />
					{:else}
						<Plate tone="warning" icon={X} label={m.sources_check_missing()} />
					{/if}
				</span>
				<span class="min-w-0 text-xs text-ink-2">
					{prefs.text(check.msg)}
					{#if !check.ok && check.missing?.length}
						<span class="block">{m.sources_check_missing_roles({ roles: '' })}<span class="font-mono">{check.missing.join(', ')}</span></span>
					{/if}
				</span>
			</li>
		{/each}
	</ul>
</div>
