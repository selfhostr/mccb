import type { Plan, PlanAccount, PlanFolder, PlanKind, PlanKindTotals } from '../api/types.ts';

export const PLAN_KINDS: PlanKind[] = ['mail', 'calendar', 'contacts'];
export const planDone = (plan: Pick<Plan, 'status'>): boolean => plan.status === 'done' || plan.status === 'failed';

export interface PlanRow {
	kind: PlanKind;
	toMigrate: number;
	items: number;
	bytes: number | null;
	estimated: boolean;
	/** Left out by a filter, all reasons together. */
	filtered: number;
	oversized: number;
}

/** One line per content kind that the dry run found. */
export function planRows(account: Pick<PlanAccount, 'kinds'>): PlanRow[] {
	const rows: PlanRow[] = [];
	for (const kind of PLAN_KINDS) {
		const k: PlanKindTotals | undefined = account.kinds?.[kind];
		if (!k) continue;
		rows.push({
			kind,
			toMigrate: k.to_migrate,
			items: k.items,
			bytes: k.to_migrate_bytes,
			estimated: !!k.bytes_estimated,
			filtered: Object.values(k.filtered ?? {}).reduce((sum, n) => sum + n, 0),
			oversized: k.oversized ?? 0
		});
	}
	return rows;
}

const folderName = (f: PlanFolder) => f.target || (Array.isArray(f.path) ? f.path.join('/') : f.path);

/** Target folders that do not exist yet, once each. `rest` is how many were cut off by `limit`. */
export function foldersToCreate(account: Pick<PlanAccount, 'folders'>, limit = 12): { names: string[]; rest: number } {
	const all = new Set<string>();
	for (const f of account.folders ?? []) if (f.create === true && !f.excluded) all.add(folderName(f));
	const names = [...all];
	return { names: names.slice(0, limit), rest: Math.max(0, names.length - limit) };
}

/** The reason to show when the dry run of an account could not read the source. */
export function planProblem(account: PlanAccount): string {
	return account.error || (account.status === 'error' ? account.source?.msg : '') || '';
}
