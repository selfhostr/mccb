import type { Batch, BatchAction, JobStatus } from '../api/types.ts';

/** The job status whose look (icon, word, colour) tells the state of a batch. */
export function batchLookStatus(b: Pick<Batch, 'state' | 'failed' | 'with_errors'>): JobStatus {
	switch (b.state) {
		case 'queued':
			return 'queued';
		case 'running':
			return 'running';
		case 'paused':
			return 'paused';
		default:
			return b.failed ? 'failed' : b.with_errors ? 'done_with_errors' : 'done';
	}
}

/** Group actions that would change something now. */
export function batchActions(b: Pick<Batch, 'by_status' | 'failed' | 'with_errors'>): BatchAction[] {
	const n = (s: JobStatus) => b.by_status[s] ?? 0;
	const out: BatchAction[] = [];
	if (n('queued') + n('running')) out.push('pause');
	if (n('paused')) out.push('resume');
	if (n('queued') + n('running') + n('paused') + n('stopping')) out.push('stop');
	if (b.failed || b.with_errors) out.push('retry-failed');
	return out;
}

export function batchItems(b: Pick<Batch, 'items'>): { done: number; total: number; errors: number } {
	const sum = { done: 0, total: 0, errors: 0 };
	for (const p of Object.values(b.items ?? {})) {
		sum.done += p?.done ?? 0;
		sum.total += p?.total ?? 0;
		sum.errors += p?.errors ?? 0;
	}
	return sum;
}
