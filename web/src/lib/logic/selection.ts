/**
 * Selection rules, in one place:
 * - selecting never goes through a filter silently: actions only touch selected rows that are visible;
 * - what is selected but hidden is counted and shown, never acted on.
 */
export type HeaderState = 'none' | 'some' | 'all';

interface IdSet {
	has(id: number): boolean;
}

export function headerState(visible: readonly number[], selected: IdSet): HeaderState {
	if (!visible.length) return 'none';
	let hit = 0;
	for (const id of visible) if (selected.has(id)) hit++;
	if (hit === 0) return 'none';
	return hit === visible.length ? 'all' : 'some';
}

export interface SelectionSplit {
	/** Selected and visible: the only rows an action may touch. */
	targets: number[];
	/** Selected but filtered out. */
	hidden: number[];
}

export function splitSelection(selected: Iterable<number>, visible: readonly number[]): SelectionSplit {
	const shown = new Set(visible);
	const targets: number[] = [];
	const hidden: number[] = [];
	for (const id of selected) (shown.has(id) ? targets : hidden).push(id);
	return { targets, hidden };
}

/** Header checkbox: "all" clears the visible rows, anything else selects every visible row. */
export function toggleAllVisible(state: HeaderState, visible: readonly number[], selected: { add(id: number): unknown; delete(id: number): unknown }): void {
	for (const id of visible) {
		if (state === 'all') selected.delete(id);
		else selected.add(id);
	}
}
