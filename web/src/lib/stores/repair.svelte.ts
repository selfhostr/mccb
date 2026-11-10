/**
 * The repair tab: the mailboxes added by hand (not the migration accounts; the duplicates tab lists them too),
 * their runs, a poll while one is active.
 */
import { SvelteSet } from 'svelte/reactivity';
import { del, get, post } from '../api/client.ts';
import type { RepairBox, RepairMode, RepairRun } from '../api/types.ts';

const ACTIVE_POLL = 1500;
const IDLE_POLL = 15000;

class RepairStore {
	boxes = $state.raw<RepairBox[]>([]);
	loaded = $state(false);
	error = $state<string | null>(null);
	selected = new SvelteSet<number>();
	testing = new SvelteSet<number>();
	#timer: ReturnType<typeof setTimeout> | null = null;
	#users = 0;

	get active(): boolean {
		return this.boxes.some((b) => b.active_run);
	}

	async load(): Promise<void> {
		try {
			this.boxes = await get<RepairBox[]>('/repair/boxes');
			this.error = null;
			const known = new Set(this.boxes.map((b) => b.id));
			for (const id of this.selected) if (!known.has(id)) this.selected.delete(id);
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

	async start(ids: number[], mode: RepairMode, folder?: string): Promise<{ runs: RepairRun[]; skipped_busy: number[] }> {
		const res = await post<{ runs: RepairRun[]; skipped_busy: number[] }>('/repair/runs', { box_ids: ids, mode, ...(folder ? { folder } : {}) });
		await this.refresh();
		return res;
	}

	async stop(runId: number): Promise<void> {
		await post(`/repair/runs/${runId}/stop`);
		await this.refresh();
	}

	async test(ids: number[]): Promise<void> {
		// One at a time per mailbox, a few in parallel: a list from a CSV can be long.
		const queue = ids.filter((id) => !this.testing.has(id));
		for (const id of queue) this.testing.add(id);
		const worker = async () => {
			for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
				try {
					await post(`/repair/boxes/${id}/test`);
				} catch {
					// the result is stored on the mailbox: the list shows it
				} finally {
					this.testing.delete(id);
				}
			}
		};
		await Promise.all(Array.from({ length: Math.min(4, queue.length) }, worker));
		await this.refresh();
	}

	async remove(id: number): Promise<void> {
		await del(`/repair/boxes/${id}`);
		this.selected.delete(id);
		await this.refresh();
	}
}

export const repairs = new RepairStore();
