import { createCoalescer, frameSchedule } from '../logic/coalesce.ts';

export type EventKind = 'job' | 'account' | 'pst';
export interface LiveEvent {
	kind: EventKind;
	data: { id: number; deleted?: boolean } & Record<string, unknown>;
}

export interface LiveHandlers {
	/** A batch of events, at most one per frame and one per object. */
	onBatch(events: LiveEvent[]): void;
	onState(connected: boolean): void;
	/** The stream came back after a drop: events were missed, reload the lists. */
	onReconnect(): void;
}

let source: EventSource | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
let wanted = false;
let attempts = 0;
let everOpen = false;

export function startEvents(handlers: LiveHandlers): void {
	wanted = true;
	if (source) return;
	const coalescer = createCoalescer<LiveEvent>(frameSchedule, (batch) => handlers.onBatch(batch));
	const es = new EventSource('/api/events');
	source = es;
	es.onopen = () => {
		attempts = 0;
		handlers.onState(true);
		if (everOpen) handlers.onReconnect();
		everOpen = true;
	};
	for (const kind of ['job', 'account', 'pst'] as EventKind[]) {
		es.addEventListener(kind, (e) => {
			try {
				const data = JSON.parse((e as MessageEvent).data);
				coalescer.push(`${kind}:${data.id}`, { kind, data });
			} catch {
				// A malformed event is dropped; the next one carries the full object.
			}
		});
	}
	es.onerror = () => {
		handlers.onState(false);
		es.close();
		source = null;
		if (!wanted) return;
		clearTimeout(timer);
		timer = setTimeout(() => startEvents(handlers), Math.min(15000, 1000 * 2 ** attempts++));
	};
}

export function stopEvents(): void {
	wanted = false;
	everOpen = false;
	clearTimeout(timer);
	source?.close();
	source = null;
}
