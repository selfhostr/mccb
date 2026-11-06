import type { Job } from '../api/types.ts';

/** The queue as the worker takes it: highest priority first, then the oldest job. */
export function queueOrder(jobs: Iterable<Pick<Job, 'id' | 'status' | 'priority'>>): number[] {
	return [...jobs]
		.filter((j) => j.status === 'queued')
		.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.id - b.id)
		.map((j) => j.id);
}

/** The queue with one job a step earlier or later, or null when it is already at that end or not queued. */
export function moveInQueue(queue: readonly number[], id: number, step: -1 | 1): number[] | null {
	const from = queue.indexOf(id);
	const to = from + step;
	if (from < 0 || to < 0 || to >= queue.length) return null;
	const next = [...queue];
	[next[from], next[to]] = [next[to], next[from]];
	return next;
}

/** Local priorities for the order the server accepted (it numbers them from the end, as reorder() does). */
export function prioritiesOf(order: readonly number[]): Map<number, number> {
	return new Map(order.map((id, i) => [id, order.length - i]));
}
