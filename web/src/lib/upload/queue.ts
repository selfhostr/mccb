/**
 * Upload queue, with no DOM and no framework. One file at a time by default, chunks in
 * parallel inside a file, SHA-256 per chunk. Same API contract as before:
 *   POST /uploads, PUT /uploads/{id}/chunks/{n} with X-Chunk-Sha256, GET /uploads/{id},
 *   POST /uploads/{id}/complete.
 */

export type UploadStatus = 'needs_file' | 'queued' | 'starting' | 'uploading' | 'completing' | 'paused' | 'error' | 'done';
export type UploadOrder = 'size-asc' | 'size-desc' | 'date' | 'name';
export const UPLOAD_ORDERS: UploadOrder[] = ['size-asc', 'size-desc', 'date', 'name'];

export interface FileLike {
	name: string;
	size: number;
	lastModified: number;
	slice(start: number, end: number): { arrayBuffer(): Promise<ArrayBuffer> };
}

interface Control {
	cancel: boolean;
	abort: AbortController;
}

export interface UploadItem {
	key: string;
	file: FileLike | null;
	filename: string;
	size: number;
	lastModified: number;
	id: number | null;
	chunkSize: number;
	total: number;
	received: Set<number>;
	sentBytes: number;
	rate: number;
	eta: number | null;
	status: UploadStatus;
	error: string | null;
	retrying: boolean;
	pstId: number | null;
	assignTo: number | null;
	control: Control | null;
	samples: [number, number][];
}

export interface StartInfo {
	id: number;
	chunk_size: number;
	received?: number[];
}

export interface QueueDeps {
	create(meta: { filename: string; size: number; last_modified: number }, signal: AbortSignal): Promise<StartInfo>;
	status(id: number, signal?: AbortSignal): Promise<{ received?: number[]; chunk_size?: number; status?: string }>;
	putChunk(id: number, n: number, body: ArrayBuffer, sha256: string, signal: AbortSignal): Promise<unknown>;
	complete(id: number, signal: AbortSignal): Promise<{ pst_id?: number; pst?: { id: number }; id?: number }>;
	remove(id: number): Promise<unknown>;
	/** May transfer the buffer to a worker: the body to send is the one returned. */
	hash(body: ArrayBuffer): Promise<{ sha256: string; body: ArrayBuffer }>;
	storage: { getItem(key: string): string | null; setItem(key: string, value: string): void };
	now(): number;
	sleep(ms: number): Promise<void>;
	onChange(): void;
	onCompleted?(item: UploadItem): void;
	onFailed?(item: UploadItem): void;
}

export const STORAGE_KEY = 'pstmig.uploads.v1';
const CHUNK_WORKERS = 3;
const MAX_TRIES = 8;
const FATAL_CHUNK = [400, 401, 403, 404, 409, 413, 416, 507];

/** Waiting items can be moved; those being sent stay where they are. */
const MOVABLE: UploadStatus[] = ['needs_file', 'queued', 'paused', 'error'];
const SENDING: UploadStatus[] = ['starting', 'uploading', 'completing'];

export const fileKey = (f: { name: string; size: number; lastModified: number }): string => `${f.name}|${f.size}|${f.lastModified}`;

function statusOf(error: unknown): number | undefined {
	const status = (error as { status?: unknown } | null)?.status;
	return typeof status === 'number' ? status : undefined;
}

const textOf = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export function sortFiles<T extends { name: string; size: number; lastModified: number }>(files: readonly T[], order: UploadOrder): T[] {
	const byName = (a: T, b: T) => a.name.localeCompare(b.name, undefined, { numeric: true });
	const list = [...files];
	switch (order) {
		case 'size-asc':
			return list.sort((a, b) => a.size - b.size || byName(a, b));
		case 'size-desc':
			return list.sort((a, b) => b.size - a.size || byName(a, b));
		case 'date':
			return list.sort((a, b) => a.lastModified - b.lastModified || byName(a, b));
		default:
			return list.sort(byName);
	}
}

export class UploadQueue {
	items: UploadItem[] = [];
	concurrency: number;
	private deps: QueueDeps;
	private running = new Set<Control>();

	constructor(deps: QueueDeps, concurrency = 1) {
		this.deps = deps;
		this.concurrency = Math.max(1, concurrency);
	}

	get(key: string): UploadItem | undefined {
		return this.items.find((i) => i.key === key);
	}

	setConcurrency(value: number): void {
		this.concurrency = Math.max(1, Math.floor(value) || 1);
		this.pump();
	}

	/** Adds files after those already waiting, sorted among themselves. Files already in the queue are skipped. */
	add(files: readonly FileLike[], options: { order?: UploadOrder; assignTo?: number | null } = {}): { added: UploadItem[]; skipped: string[] } {
		const added: UploadItem[] = [];
		const skipped: string[] = [];
		for (const file of sortFiles(files, options.order ?? 'size-asc')) {
			const key = fileKey(file);
			const existing = this.get(key);
			if (existing?.status === 'done') {
				this.items = this.items.filter((i) => i !== existing);
			} else if (existing?.status === 'needs_file') {
				existing.file = file;
				existing.status = 'queued';
				if (options.assignTo != null) existing.assignTo = options.assignTo;
				added.push(existing);
				continue;
			} else if (existing) {
				skipped.push(file.name);
				continue;
			}
			const item = this.newItem(key, file.name, file.size, file.lastModified, file, options.assignTo ?? null);
			item.status = 'queued';
			this.items.push(item);
			added.push(item);
		}
		this.persist();
		this.changed();
		this.pump();
		return { added, skipped };
	}

	pause(key: string): void {
		const item = this.get(key);
		if (!item) return;
		if (item.status === 'queued') {
			item.status = 'paused';
		} else if (item.status === 'starting' || item.status === 'uploading') {
			this.stop(item);
			item.status = 'paused';
			item.rate = 0;
			item.eta = null;
		} else {
			return;
		}
		this.changed();
		this.pump();
	}

	resume(key: string): void {
		const item = this.get(key);
		if (!item || !item.file || (item.status !== 'paused' && item.status !== 'error')) return;
		item.status = 'queued';
		item.error = null;
		this.changed();
		this.pump();
	}

	pauseAll(): void {
		// The waiting ones first: pausing the active file must not start the next one on the way.
		for (const item of this.items) if (item.status === 'queued') item.status = 'paused';
		for (const item of this.items) if (item.status === 'starting' || item.status === 'uploading') this.pause(item.key);
		this.changed();
	}

	resumeAll(): void {
		for (const item of this.items) if (item.status === 'paused') this.resume(item.key);
	}

	/** Stops the upload and frees the space it holds on the server. */
	cancel(key: string): void {
		const item = this.get(key);
		if (!item || item.status === 'completing') return;
		this.stop(item);
		this.items = this.items.filter((i) => i !== item);
		this.persist();
		if (item.id != null && item.status !== 'done') this.deps.remove(item.id).catch(() => {});
		this.changed();
		this.pump();
	}

	dismiss(key: string): void {
		const item = this.get(key);
		if (item?.status !== 'done') return;
		this.items = this.items.filter((i) => i !== item);
		this.changed();
	}

	clearDone(): void {
		this.items = this.items.filter((i) => i.status !== 'done');
		this.changed();
	}

	/** One step up or down among the items that can move. */
	move(key: string, delta: -1 | 1): void {
		const from = this.items.findIndex((i) => i.key === key);
		if (from < 0 || !MOVABLE.includes(this.items[from].status)) return;
		for (let to = from + delta; to >= 0 && to < this.items.length; to += delta) {
			if (MOVABLE.includes(this.items[to].status)) {
				[this.items[from], this.items[to]] = [this.items[to], this.items[from]];
				this.persist();
				this.changed();
				return;
			}
		}
	}

	/** Drag and drop: puts `key` at the position of `target`. */
	moveTo(key: string, target: string): void {
		const from = this.items.findIndex((i) => i.key === key);
		const to = this.items.findIndex((i) => i.key === target);
		if (from < 0 || to < 0 || from === to) return;
		if (!MOVABLE.includes(this.items[from].status) || !MOVABLE.includes(this.items[to].status)) return;
		const [item] = this.items.splice(from, 1);
		this.items.splice(to, 0, item);
		this.persist();
		this.changed();
	}

	/** Re-sorts the waiting items in place; what is being sent does not move. */
	resort(order: UploadOrder): void {
		const slots: number[] = [];
		const waiting: UploadItem[] = [];
		this.items.forEach((item, i) => {
			if (MOVABLE.includes(item.status)) {
				slots.push(i);
				waiting.push(item);
			}
		});
		const sorted = sortFiles(
			waiting.map((item) => ({ name: item.filename, size: item.size, lastModified: item.lastModified, item })),
			order
		);
		slots.forEach((slot, i) => (this.items[slot] = sorted[i].item));
		this.persist();
		this.changed();
	}

	/** After a reload the files are gone: list what was known, the user selects them again. */
	async restore(): Promise<void> {
		for (const record of this.loadRecords()) {
			if (this.get(record.key)) continue;
			const item = this.newItem(record.key, record.filename, record.size, record.lastModified, null, record.assignTo ?? null);
			item.id = record.id ?? null;
			item.status = 'needs_file';
			this.items.push(item);
		}
		this.changed();
		await Promise.all(
			this.items
				.filter((i) => i.status === 'needs_file' && i.id != null)
				.map(async (item) => {
					try {
						const info = await this.deps.status(item.id as number);
						if (info.status && info.status !== 'uploading') throw Object.assign(new Error('finished'), { status: 404 });
						item.chunkSize = info.chunk_size ?? 0;
						item.received = new Set(info.received ?? []);
						item.total = item.chunkSize ? Math.max(1, Math.ceil(item.size / item.chunkSize)) : 0;
						item.sentBytes = this.bytesDone(item);
					} catch (error) {
						if (statusOf(error) === 404) this.items = this.items.filter((i) => i !== item);
					}
				})
		);
		this.persist();
		this.changed();
	}

	/** `true` while leaving the page would interrupt something. */
	get busy(): boolean {
		return this.items.some((i) => SENDING.includes(i.status));
	}

	private newItem(key: string, filename: string, size: number, lastModified: number, file: FileLike | null, assignTo: number | null): UploadItem {
		return {
			key,
			file,
			filename,
			size,
			lastModified,
			id: null,
			chunkSize: 0,
			total: 0,
			received: new Set(),
			sentBytes: 0,
			rate: 0,
			eta: null,
			status: 'queued',
			error: null,
			retrying: false,
			pstId: null,
			assignTo,
			control: null,
			samples: []
		};
	}

	private changed(): void {
		this.deps.onChange();
	}

	private stop(item: UploadItem): void {
		if (!item.control) return;
		item.control.cancel = true;
		item.control.abort.abort();
		item.control = null;
	}

	private pump(): void {
		while (this.running.size < this.concurrency) {
			const next = this.items.find((i) => i.status === 'queued' && i.file);
			if (!next) return;
			const control: Control = { cancel: false, abort: new AbortController() };
			next.control = control;
			next.status = 'starting';
			this.running.add(control);
			void this.run(next, control).finally(() => {
				this.running.delete(control);
				this.pump();
			});
		}
	}

	private bytesDone(item: UploadItem): number {
		if (!item.chunkSize) return 0;
		let bytes = 0;
		for (const n of item.received) bytes += Math.min(item.chunkSize, item.size - n * item.chunkSize);
		return bytes;
	}

	private async run(item: UploadItem, control: Control): Promise<void> {
		const signal = control.abort.signal;
		try {
			item.error = null;
			this.changed();
			const info = await this.retrying(control, () => this.deps.create({ filename: item.filename, size: item.size, last_modified: item.lastModified }, signal));
			if (control.cancel) return;
			item.id = info.id;
			item.chunkSize = info.chunk_size;
			item.total = Math.max(1, Math.ceil(item.size / item.chunkSize));
			item.received = new Set(info.received ?? []);
			item.sentBytes = this.bytesDone(item);
			item.samples = [[this.deps.now(), item.sentBytes]];
			item.status = 'uploading';
			this.persist();
			this.changed();

			// The server's word is final: chunks it reports missing are sent again.
			for (;;) {
				await this.sendMissing(item, control);
				if (control.cancel) return;
				const state = await this.retrying(control, () => this.deps.status(item.id as number, signal));
				if (control.cancel) return;
				const have = new Set(state.received ?? []);
				let missing = false;
				for (let n = 0; n < item.total; n++) if (!have.has(n)) missing = true;
				if (!missing) break;
				item.received = have;
				item.sentBytes = this.bytesDone(item);
			}

			item.status = 'completing';
			this.changed();
			const done = await this.retrying(control, () => this.deps.complete(item.id as number, signal));
			if (control.cancel) return;
			item.pstId = done.pst_id ?? done.pst?.id ?? done.id ?? item.id;
			item.status = 'done';
			item.control = null;
			item.rate = 0;
			item.eta = 0;
			item.sentBytes = item.size;
			item.file = null;
			this.persist();
			this.changed();
			this.deps.onCompleted?.(item);
		} catch (error) {
			if (control.cancel) return;
			item.status = 'error';
			item.control = null;
			item.error = textOf(error);
			item.rate = 0;
			item.eta = null;
			this.changed();
			this.deps.onFailed?.(item);
		}
	}

	private async sendMissing(item: UploadItem, control: Control): Promise<void> {
		const todo: number[] = [];
		for (let n = 0; n < item.total; n++) if (!item.received.has(n)) todo.push(n);
		let failed: unknown = null;
		const worker = async () => {
			while (!control.cancel && failed === null && todo.length) {
				const n = todo.shift() as number;
				try {
					await this.sendChunk(item, control, n);
				} catch (error) {
					if (failed === null) failed = error;
				}
			}
		};
		await Promise.all(Array.from({ length: CHUNK_WORKERS }, worker));
		if (control.cancel) return;
		if (failed !== null) throw failed;
	}

	private async sendChunk(item: UploadItem, control: Control, n: number): Promise<void> {
		const start = n * item.chunkSize;
		const end = Math.min(item.size, start + item.chunkSize);
		const raw = await (item.file as FileLike).slice(start, end).arrayBuffer();
		if (control.cancel) return;
		const { sha256, body } = await this.deps.hash(raw);
		for (let attempt = 0; ; attempt++) {
			if (control.cancel) return;
			try {
				await this.deps.putChunk(item.id as number, n, body, sha256, control.abort.signal);
				if (control.cancel) return;
				item.received.add(n);
				item.sentBytes += end - start;
				item.retrying = false;
				this.sample(item);
				this.changed();
				return;
			} catch (error) {
				if (control.cancel) return;
				const status = statusOf(error);
				if ((status !== undefined && FATAL_CHUNK.includes(status)) || attempt + 1 >= MAX_TRIES) throw error;
				item.retrying = true;
				this.changed();
				await this.backoff(control, attempt);
			}
		}
	}

	private async retrying<T>(control: Control, fn: () => Promise<T>): Promise<T> {
		for (let attempt = 0; ; attempt++) {
			try {
				return await fn();
			} catch (error) {
				if (control.cancel) throw error;
				const status = statusOf(error);
				const fatal = status !== undefined && status < 500 && status !== 408 && status !== 429;
				if (fatal || status === 507 || attempt + 1 >= MAX_TRIES) throw error;
				await this.backoff(control, attempt);
			}
		}
	}

	private async backoff(control: Control, attempt: number): Promise<void> {
		const total = Math.min(20000, 800 * 2 ** attempt) * (0.7 + Math.random() * 0.6);
		const step = 200;
		for (let waited = 0; waited < total && !control.cancel; waited += step) await this.deps.sleep(step);
	}

	private sample(item: UploadItem): void {
		const now = this.deps.now();
		item.samples.push([now, item.sentBytes]);
		while (item.samples.length > 2 && now - item.samples[0][0] > 8000) item.samples.shift();
		const [t0, b0] = item.samples[0];
		const seconds = (now - t0) / 1000;
		if (seconds > 0.5) {
			item.rate = (item.sentBytes - b0) / seconds;
			item.eta = item.rate > 0 ? (item.size - item.sentBytes) / item.rate : null;
		}
	}

	private loadRecords(): { key: string; id?: number | null; filename: string; size: number; lastModified: number; assignTo?: number | null; pos?: number; ts?: number }[] {
		try {
			const parsed = JSON.parse(this.deps.storage.getItem(STORAGE_KEY) ?? '{}');
			const records = Object.values(parsed) as ReturnType<UploadQueue['loadRecords']>;
			return records.sort((a, b) => (a.pos ?? a.ts ?? 0) - (b.pos ?? b.ts ?? 0));
		} catch {
			return [];
		}
	}

	private persist(): void {
		const records: Record<string, unknown> = {};
		this.items.forEach((item, pos) => {
			if (item.status === 'done') return;
			records[item.key] = { key: item.key, id: item.id, filename: item.filename, size: item.size, lastModified: item.lastModified, assignTo: item.assignTo, pos };
		});
		try {
			this.deps.storage.setItem(STORAGE_KEY, JSON.stringify(records));
		} catch {
			// Storage full or blocked: uploads still run, only the resume list is lost.
		}
	}
}
