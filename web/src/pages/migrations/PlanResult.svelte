<script lang="ts">
	/** The answer of a dry run: per mailbox, what would be copied, how big, where, and what to read first. */
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { m } from '#lib/paraglide/messages.js';
	import type { PlanAccount, PlanAccountStatus, PlanResult } from '../../lib/api/types.ts';
	import { fmtBytes, fmtNum } from '../../lib/format.ts';
	import { foldersToCreate, planProblem, planRows } from '../../lib/logic/plan.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import Notice from '../../lib/ui/Notice.svelte';
	import Plate from '../../lib/ui/Plate.svelte';

	let { result }: { result: PlanResult } = $props();

	const LOOK = $derived({
		ok: { tone: 'signal' as const, icon: CircleCheck, label: m.plan_status_ok() },
		warning: { tone: 'advisory' as const, icon: CircleAlert, label: m.plan_status_warning() },
		error: { tone: 'warning' as const, icon: TriangleAlert, label: m.plan_status_error() }
	} satisfies Record<PlanAccountStatus, unknown>);
	const KIND = $derived({ mail: m.migrations_row_content_mail(), calendar: m.migrations_row_content_calendar(), contacts: m.migrations_row_content_contacts() });

	const toMigrate = (a: PlanAccount) => planRows(a).reduce((sum, r) => sum + r.toMigrate, 0);
	const bytesOf = (a: PlanAccount) => planRows(a).reduce((sum, r) => sum + (r.bytes ?? 0), 0);
</script>

<div class="flex flex-col gap-2" data-testid="plan-result">
	<p class="m-0 font-medium" data-testid="plan-totals">
		{m.plan_totals({ accounts: result.totals.accounts, items: fmtNum(result.totals.to_migrate), size: fmtBytes(result.totals.to_migrate_bytes) })}
	</p>
	<ul class="m-0 flex list-none flex-col p-0">
		{#each result.accounts as account (account.account_id)}
			{@const look = LOOK[account.status] ?? LOOK.error}
			{@const rows = planRows(account)}
			{@const create = foldersToCreate(account)}
			{@const problem = planProblem(account)}
			<li class="border-b border-line" data-testid="plan-account">
				<details>
					<summary class="flex min-h-10 cursor-pointer flex-wrap items-center gap-x-2 py-1">
						<span class="min-w-0 truncate font-mono text-xs font-medium">{prefs.email(account.email ?? `#${account.account_id}`)}</span>
						<Plate tone={look.tone} icon={look.icon} label={look.label} />
						{#if rows.length}<span class="text-xs text-ink-2">{m.plan_account_line({ items: fmtNum(toMigrate(account)), size: fmtBytes(bytesOf(account)) })}</span>{/if}
					</summary>
					<div class="flex flex-col gap-2 pb-3 pl-4 text-xs">
						{#if problem}<Notice><p class="m-0 font-mono">{prefs.text(problem)}</p></Notice>{/if}
						{#if rows.length}
							<div class="overflow-x-auto">
								<table class="w-full">
									<thead>
										<tr class="text-left text-ink-2">
											<th class="py-1 pr-2 font-medium">{m.jobs_report_type()}</th>
											<th class="py-1 pr-2 text-right font-medium">{m.plan_col_to_migrate()}</th>
											<th class="py-1 pr-2 text-right font-medium">{m.plan_col_size()}</th>
											<th class="py-1 pr-2 text-right font-medium">{m.plan_col_filtered()}</th>
											<th class="py-1 text-right font-medium">{m.plan_col_oversized()}</th>
										</tr>
									</thead>
									<tbody class="font-mono">
										{#each rows as row (row.kind)}
											<tr class="border-t border-line">
												<td class="py-1 pr-2 font-sans">{KIND[row.kind]}</td>
												<td class="py-1 pr-2 text-right">{fmtNum(row.toMigrate)}</td>
												<td class="py-1 pr-2 text-right">{row.bytes == null ? '-' : fmtBytes(row.bytes)}</td>
												<td class="py-1 pr-2 text-right">{fmtNum(row.filtered)}</td>
												<td class="py-1 text-right">{fmtNum(row.oversized)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
						{#if account.quota}
							<p class="m-0 flex flex-wrap items-center gap-2" data-testid="plan-quota">
								<Plate
									tone={account.quota.enough ? 'signal' : 'warning'}
									icon={account.quota.enough ? CircleCheck : TriangleAlert}
									label={account.quota.enough ? m.plan_quota_enough() : m.plan_quota_short()}
								/>
								<span>{m.plan_quota({ need: fmtBytes(account.quota.need), free: fmtBytes(account.quota.free) })}</span>
							</p>
						{/if}
						{#if create.names.length}
							<p class="m-0" data-testid="plan-create">
								<span class="font-medium">{m.plan_create({ count: create.names.length + create.rest })}</span>
								<span class="font-mono break-words">{create.names.map((n) => prefs.text(n)).join(', ')}{create.rest ? ` +${create.rest}` : ''}</span>
							</p>
						{/if}
						{#each account.warnings ?? [] as warning, i (i)}
							<Notice><p class="m-0">{prefs.text(warning)}</p></Notice>
						{/each}
						{#each account.notes ?? [] as note, i (i)}
							<p class="m-0 text-ink-3">{prefs.text(note)}</p>
						{/each}
					</div>
				</details>
			</li>
		{/each}
	</ul>
</div>
