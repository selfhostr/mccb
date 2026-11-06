import type { Account, Job, Pst, TestResult } from '../api/types.ts';
import { hasCustomSource, hasOwnTarget } from './connector.ts';

export const ACTIVE_JOB = ['queued', 'running', 'paused', 'stopping'];
export const isActive = (job: Job | undefined | null): boolean => !!job && ACTIVE_JOB.includes(job.status);

export type StatusFilter = '' | 'untested' | 'test_failed' | 'no_pst' | 'active' | 'done' | 'problem';
export const STATUS_FILTERS: StatusFilter[] = ['', 'untested', 'test_failed', 'no_pst', 'active', 'done', 'problem'];

/** A password change sets the status back to `new` and leaves the old result in place: it no longer applies. */
export function currentResult(account: Pick<Account, 'status' | 'last_test_result'>): TestResult | null {
	return account.status === 'new' ? null : (account.last_test_result ?? null);
}

export function testPassed(result: TestResult): boolean {
	return [result.imap, result.caldav, result.carddav].every((r) => !r || r.ok);
}

export function needsAttention(account: Account, job: Job | undefined): boolean {
	return (
		account.status === 'auth_failed' ||
		account.status === 'error' ||
		job?.status === 'failed' ||
		job?.status === 'done_with_errors'
	);
}

export function matchesFilter(
	account: Account,
	job: Job | undefined,
	filter: { q: string; server: number | null; status: StatusFilter }
): boolean {
	if (filter.server && account.server_id !== filter.server) return false;
	if (filter.q && !`${account.email} ${account.login ?? ''}`.toLowerCase().includes(filter.q)) return false;
	switch (filter.status) {
		case 'untested':
			return account.status === 'new';
		case 'test_failed':
			return account.status === 'auth_failed' || account.status === 'error';
		case 'no_pst':
			return account.pst_id == null && !hasCustomSource(account);
		case 'active':
			return isActive(job);
		case 'done':
			return job?.status === 'done';
		case 'problem':
			return needsAttention(account, job);
		default:
			return true;
	}
}

export interface Summary {
	total: number;
	running: number;
	done: number;
	attention: number;
}

export function summarize(accounts: Iterable<Account>, jobOf: (id: number) => Job | undefined): Summary {
	const s: Summary = { total: 0, running: 0, done: 0, attention: 0 };
	for (const account of accounts) {
		const job = jobOf(account.id);
		s.total++;
		if (isActive(job)) s.running++;
		if (job?.status === 'done') s.done++;
		if (needsAttention(account, job)) s.attention++;
	}
	return s;
}

export interface ImportPlan {
	ready: Account[];
	noPst: Account[];
	noPassword: Account[];
	active: Account[];
}

/** What pressing Import would do, said before it happens. */
export function importPlan(accounts: Account[], jobOf: (id: number) => Job | undefined, pstOf: (id: number) => Pst | undefined): ImportPlan {
	const plan: ImportPlan = { ready: [], noPst: [], noPassword: [], active: [] };
	for (const account of accounts) {
		const pst = account.pst_id != null ? pstOf(account.pst_id) : undefined;
		if (isActive(jobOf(account.id))) plan.active.push(account);
		// A connector source needs no PST; an own target brings its own credentials.
		else if (!hasCustomSource(account) && (!pst || pst.status !== 'valid')) plan.noPst.push(account);
		else if (!account.has_password && !(hasOwnTarget(account) && account.target?.has_secret)) plan.noPassword.push(account);
		else plan.ready.push(account);
	}
	return plan;
}
