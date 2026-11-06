import type { Account, Pst, PstChain } from '../api/types.ts';

export const FETCH_ACTIVE = ['fetching', 'cancelling'];
export const isFetching = (p: Pick<Pst, 'status'>) => FETCH_ACTIVE.includes(p.status);

/** Share of a fetch received, or null while the total is unknown (size is 0). */
export function fetchPercent(p: Pick<Pst, 'size' | 'received_bytes'>): number | null {
	if (!p.size || p.size <= 0) return null;
	return Math.max(0, Math.min(100, Math.round(((p.received_bytes ?? 0) / p.size) * 100)));
}

export type ChainCode = 'assigned' | 'ambiguous' | 'no_match' | 'account_has_pst' | 'already_assigned' | 'error' | 'other';

export interface ChainView {
	code: ChainCode;
	/** The raw result when the code is `other`, or the error text. */
	detail: string;
	accountEmail: string | null;
	accountId: number | null;
	started: boolean;
}

const KNOWN: ChainCode[] = ['assigned', 'ambiguous', 'no_match', 'account_has_pst', 'already_assigned', 'error'];

/** The automatic assignment in a form the row can put into words. Null when nothing was tried. */
export function chainView(chain: PstChain | null | undefined, accountOf: (id: number) => Pick<Account, 'email'> | undefined): ChainView | null {
	if (!chain || typeof chain.result !== 'string') return null;
	const code = (KNOWN as string[]).includes(chain.result) ? (chain.result as ChainCode) : 'other';
	const detail = code === 'other' ? chain.result : typeof chain.error === 'string' ? chain.error : '';
	const accountEmail = chain.account_id != null ? (accountOf(chain.account_id)?.email ?? null) : null;
	return { code, detail, accountEmail, accountId: chain.account_id ?? null, started: chain.job_id != null };
}
