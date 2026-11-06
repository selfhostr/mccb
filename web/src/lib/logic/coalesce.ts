/**
 * Live events arrive in bursts. Only the latest object per (kind, id) matters, and the
 * UI applies them at most once per frame: nothing here touches the DOM.
 */
export type Schedule = (run: () => void) => void;

export interface Coalescer<T> {
	push(key: string, value: T): void;
	flushNow(): void;
	readonly size: number;
}

export function createCoalescer<T>(schedule: Schedule, apply: (batch: T[]) => void): Coalescer<T> {
	const pending = new Map<string, T>();
	let planned = false;

	const flush = () => {
		planned = false;
		if (!pending.size) return;
		const batch = [...pending.values()];
		pending.clear();
		apply(batch);
	};

	return {
		push(key, value) {
			// Delete first: the batch keeps the order of the most recent events.
			pending.delete(key);
			pending.set(key, value);
			if (planned) return;
			planned = true;
			schedule(flush);
		},
		flushNow: flush,
		get size() {
			return pending.size;
		}
	};
}

/** One frame when the tab is visible; requestAnimationFrame stops in a hidden tab, so fall back to a timer. */
export function frameSchedule(run: () => void): void {
	if (typeof document !== 'undefined' && !document.hidden && typeof requestAnimationFrame === 'function') {
		requestAnimationFrame(run);
	} else {
		setTimeout(run, 250);
	}
}
