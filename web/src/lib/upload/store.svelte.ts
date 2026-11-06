import { m } from '#lib/paraglide/messages.js';
import { del, get, post, put } from '../api/client.ts';
import type { UploadInfo } from '../api/types.ts';
import { frameSchedule } from '../logic/coalesce.ts';
import { prefs } from '../prefs.svelte.ts';
import { toast } from '../ui/toast.svelte.ts';
import { hashChunk } from './hasher.ts';
import { type FileLike, type UploadItem, type UploadOrder, UploadQueue } from './queue.ts';

/** A plain copy of a queue item for the UI. */
export interface UploadView {
	key: string;
	filename: string;
	size: number;
	lastModified: number;
	status: UploadItem['status'];
	sentBytes: number;
	rate: number;
	eta: number | null;
	error: string | null;
	retrying: boolean;
	hasFile: boolean;
	pstId: number | null;
}

const ASSIGN_KEY = 'mccb.uploads.assign.v1';

function loadAssignments(): Record<string, number> {
	try {
		return JSON.parse(localStorage.getItem(ASSIGN_KEY) ?? '{}');
	} catch {
		return {};
	}
}

function saveAssignments(map: Record<string, number>): void {
	try {
		localStorage.setItem(ASSIGN_KEY, JSON.stringify(map));
	} catch {
		// Storage blocked: the file still arrives, it just has to be linked by hand.
	}
}

class Uploads {
	items = $state.raw<UploadView[]>([]);
	private planned = false;
	private queue: UploadQueue;

	constructor() {
		this.queue = new UploadQueue(
			{
				create: (meta, signal) => post('/uploads', meta, { signal }),
				status: (id, signal) => get<UploadInfo>(`/uploads/${id}`, { signal }),
				putChunk: (id, n, body, sha256, signal) =>
					put(`/uploads/${id}/chunks/${n}`, body, { signal, headers: { 'X-Chunk-Sha256': sha256, 'Content-Type': 'application/octet-stream' } }),
				complete: (id, signal) => post(`/uploads/${id}/complete`, undefined, { signal }),
				remove: (id) => del(`/psts/${id}`),
				hash: hashChunk,
				storage: localStorage,
				now: () => performance.now(),
				sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
				onChange: () => this.refresh(),
				onCompleted: (item) => {
					if (item.assignTo != null && item.pstId != null) {
						// Linked when the analysis says the file is valid, even after a reload.
						saveAssignments({ ...loadAssignments(), [item.pstId]: item.assignTo });
					}
					toast(m.files_queue_uploaded({ name: prefs.file(item.filename) }), 'signal');
				},
				onFailed: (item) => toast(m.files_queue_failed({ name: prefs.file(item.filename), error: item.error ?? '' }), 'warning', 8000)
			},
			prefs.value.uploadConcurrency
		);
		void this.queue.restore();
		addEventListener('beforeunload', (e) => {
			if (this.queue.busy) e.preventDefault();
		});
		addEventListener('online', () => {
			for (const item of this.queue.items) if (item.status === 'error') this.queue.resume(item.key);
		});
	}

	private refresh(): void {
		if (this.planned) return;
		this.planned = true;
		frameSchedule(() => {
			this.planned = false;
			this.items = this.queue.items.map((i) => ({
				key: i.key,
				filename: i.filename,
				size: i.size,
				lastModified: i.lastModified,
				status: i.status,
				sentBytes: i.sentBytes,
				rate: i.rate,
				eta: i.eta,
				error: i.error,
				retrying: i.retrying,
				hasFile: !!i.file,
				pstId: i.pstId
			}));
		});
	}

	add(files: readonly FileLike[], order: UploadOrder, assignTo: number | null = null): void {
		const { skipped } = this.queue.add(files, { order, assignTo });
		for (const name of skipped) toast(m.files_queue_already({ name: prefs.file(name) }), 'advisory');
	}

	/** Called when an analysis ends. Returns the account to link, once. */
	takeAssignment(pstId: number): number | null {
		const map = loadAssignments();
		const account = map[pstId];
		if (account === undefined) return null;
		delete map[pstId];
		saveAssignments(map);
		return account;
	}

	pause = (key: string) => this.queue.pause(key);
	resume = (key: string) => this.queue.resume(key);
	cancel = (key: string) => this.queue.cancel(key);
	dismiss = (key: string) => this.queue.dismiss(key);
	move = (key: string, delta: -1 | 1) => this.queue.move(key, delta);
	moveTo = (key: string, target: string) => this.queue.moveTo(key, target);
	resort = (order: UploadOrder) => this.queue.resort(order);
	pauseAll = () => this.queue.pauseAll();
	resumeAll = () => this.queue.resumeAll();
	clearDone = () => this.queue.clearDone();
	setConcurrency = (value: number) => this.queue.setConcurrency(value);
}

export const uploads = new Uploads();
