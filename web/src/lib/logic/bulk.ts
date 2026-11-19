import type { Job, JobStatus } from '../api/types.ts';

/** What can be done at once to the jobs of the selected mailboxes: the per-job endpoints of `/jobs/:id/:action`. */
export type BulkJobAction = 'pause' | 'resume' | 'stop' | 'retry-errors';

export const BULK_JOB_ACTIONS: BulkJobAction[] = ['pause', 'resume', 'stop', 'retry-errors'];

/** The same rules as the job panel: which states each action applies to. */
const APPLIES: Record<BulkJobAction, JobStatus[]> = {
	pause: ['queued', 'running'],
	resume: ['paused'],
	stop: ['queued', 'running', 'paused'],
	'retry-errors': ['done_with_errors', 'stopped', 'failed']
};

export function jobsFor(action: BulkJobAction, jobs: Iterable<Pick<Job, 'id' | 'status'> | undefined>): number[] {
	const ids: number[] = [];
	for (const job of jobs) if (job && APPLIES[action].includes(job.status)) ids.push(job.id);
	return ids;
}

/** Only the actions that would change something for at least one of these jobs. */
export function bulkJobActions(jobs: Iterable<Pick<Job, 'id' | 'status'> | undefined>): BulkJobAction[] {
	const list = [...jobs];
	return BULK_JOB_ACTIONS.filter((action) => jobsFor(action, list).length > 0);
}
