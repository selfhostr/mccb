/**
 * The duplicates tab: the migration accounts and the mailboxes added by hand (the list of the repair store, which
 * adds, edits and removes them), their runs, a poll while one is active.
 */
import { SvelteSet } from 'svelte/reactivity';
import { get, post } from '../api/client.ts';
import type { DedupBox, DedupKind, DedupMode, DedupRun } from '../api/types.ts';

const ACTIVE_POLL = 1500;
const IDLE_POLL = 15000;

class DuplicatesStore {
	boxes = $state.raw<DedupBox[]>([]);
	loaded = $state(false);
	error = $state<string | null>(null);
	selected = new SvelteSet<string>();
	/** The kinds a started run covers. */
	kinds = new SvelteSet<DedupKind>(['mail', 'calendar', 'contacts']);
	#timer: ReturnType<typeof setTimeout> | null = null;
	#users = 0;

	get active(): boolean {
		return this.boxes.some((b) => b.active_run);
	}

	async load(): Promise<void> {
		try {
			this.boxes = await get<DedupBox[]>('/dedup/boxes');
			this.error = null;
			const known = new Set(this.boxes.map((b) => b.key));
			for (const key of this.selected) if (!known.has(key)) this.selected.delete(key);
		} catch (err) {
			this.error = err instanceof Error ? err.message : String(err);
		} finally {
			this.loaded = true;
		}
	}

	/** The page calls this while it is shown: one poll for every reader, stopped when the last one leaves. */
	follow(): () => void {
		this.#users++;
		if (this.#users === 1) void this.#tick();
		return () => {
			this.#users--;
			if (this.#users === 0 && this.#timer) {
				clearTimeout(this.#timer);
				this.#timer = null;
			}
		};
	}

	async #tick(): Promise<void> {
		await this.load();
		if (this.#users === 0) return;
		this.#timer = setTimeout(() => void this.#tick(), this.active ? ACTIVE_POLL : IDLE_POLL);
	}

	/** After an action: read again now, and keep the fast pace while something runs. */
	async refresh(): Promise<void> {
		if (this.#timer) clearTimeout(this.#timer);
		if (this.#users > 0) await this.#tick();
		else await this.load();
	}

	/** Removal is never started without `confirm`: the server refuses it otherwise. */
	async start(keys: string[], mode: DedupMode, kinds: DedupKind[]): Promise<{ runs: DedupRun[]; skipped_busy: string[] }> {
		const res = await post<{ runs: DedupRun[]; skipped_busy: string[] }>('/dedup/runs', { box_keys: keys, mode, kinds, ...(mode === 'dedup' ? { confirm: true } : {}) });
		await this.refresh();
		return res;
	}

	async stop(runId: number): Promise<void> {
		await post(`/dedup/runs/${runId}/stop`);
		await this.refresh();
	}
}

export const duplicates = new DuplicatesStore();
