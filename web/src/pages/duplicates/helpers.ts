import { m } from '#lib/paraglide/messages.js';
import type { DedupBox, DedupFolder, DedupKind } from '../../lib/api/types.ts';

export const KINDS: DedupKind[] = ['mail', 'calendar', 'contacts'];

export function kindLabel(kind: DedupKind): string {
	return kind === 'mail' ? m.dedup_kind_mail() : kind === 'calendar' ? m.dedup_kind_calendar() : m.dedup_kind_contacts();
}

/** The count of one kind, with its noun ("37 events"). */
export function kindCount(kind: DedupKind, count: number): string {
	return kind === 'mail' ? m.dedup_count_mail({ count }) : kind === 'calendar' ? m.dedup_count_calendar({ count }) : m.dedup_count_contacts({ count });
}

export function noteLabel(note: string): string {
	if (note === 'no_uidplus') return m.dedup_note_no_uidplus();
	if (note === 'dav_disabled') return m.dedup_note_dav_disabled();
	return note;
}

/** Why a folder or collection was left alone. */
export function refusedLabel(refused: string): string {
	if (refused === 'no_uidplus') return m.dedup_refused_no_uidplus();
	if (refused === 'no_etag') return m.dedup_refused_no_etag();
	return refused;
}

export interface Plan {
	/** Duplicates per kind, summed over the boxes that will run. */
	counts: Record<DedupKind, number>;
	/** The boxes that will run: analysed, free, with something to remove in the chosen kinds. */
	keys: string[];
	/** Selected boxes left out because they were never analysed. */
	unanalysed: number;
}

/** What "Remove duplicates" would do for these boxes, from their last analysis. Exactly what the dialog states. */
export function planRemoval(boxes: DedupBox[], kinds: Iterable<DedupKind>): Plan {
	const chosen = [...kinds];
	const plan: Plan = { counts: { mail: 0, calendar: 0, contacts: 0 }, keys: [], unanalysed: 0 };
	for (const box of boxes) {
		if (!box.last_scan) {
			plan.unanalysed++;
			continue;
		}
		if (box.active_run || box.busy || !box.usable) continue;
		let some = 0;
		for (const kind of chosen) {
			const n = box.last_scan.totals[kind]?.duplicates ?? 0;
			plan.counts[kind] += n;
			some += n;
		}
		if (some > 0) plan.keys.push(box.key);
	}
	return plan;
}

/** The part of a run's totals that matters now: to delete (scan) or deleted (removal). */
export function folderCount(f: DedupFolder, mode: 'scan' | 'dedup'): number {
	return mode === 'scan' ? f.duplicates : f.deleted;
}
