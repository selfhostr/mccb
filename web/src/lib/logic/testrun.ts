/**
 * Runs account tests with a concurrency limit. An account already waiting or running
 * cannot be queued again, so a second click never starts a second test.
 */
export type TestPhase = 'waiting' | 'running';

export interface BatchSummary<R> {
	total: number;
	results: Map<number, R | Error>;
}

export interface TestRunner {
	enqueue(ids: readonly number[]): number[];
	phase(id: number): TestPhase | undefined;
	readonly pending: number;
}

export function createTestRunner<R>(options: {
	run: (id: number) => Promise<R>;
	concurrency?: number;
	onPhase?: (id: number, phase: TestPhase | undefined) => void;
	onBatchDone?: (summary: BatchSummary<R>) => void;
}): TestRunner {
	const limit = options.concurrency ?? 4;
	const phases = new Map<number, TestPhase>();
	const waiting: number[] = [];
	let running = 0;
	let results = new Map<number, R | Error>();

	const set = (id: number, phase: TestPhase | undefined) => {
		if (phase) phases.set(id, phase);
		else phases.delete(id);
		options.onPhase?.(id, phase);
	};

	const pump = () => {
		while (running < limit && waiting.length) {
			const id = waiting.shift() as number;
			running++;
			set(id, 'running');
			options
				.run(id)
				.then(
					(r) => results.set(id, r),
					(e) => results.set(id, e instanceof Error ? e : new Error(String(e)))
				)
				.finally(() => {
					running--;
					set(id, undefined);
					pump();
					if (!running && !waiting.length) {
						const done = results;
						results = new Map();
						options.onBatchDone?.({ total: done.size, results: done });
					}
				});
		}
	};

	return {
		enqueue(ids) {
			const added: number[] = [];
			for (const id of ids) {
				if (phases.has(id)) continue;
				waiting.push(id);
				set(id, 'waiting');
				added.push(id);
			}
			pump();
			return added;
		},
		phase: (id) => phases.get(id),
		get pending() {
			return phases.size;
		}
	};
}
