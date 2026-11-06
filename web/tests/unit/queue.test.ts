import { describe, expect, it } from 'vitest';
import { type FileLike, sortFiles, STORAGE_KEY, type QueueDeps, UploadQueue } from '../../src/lib/upload/queue.ts';
import { sha256Js } from '../../src/lib/upload/sha256.ts';

const CHUNK = 10;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

function file(name: string, size = 30, lastModified = 1): FileLike {
	return { name, size, lastModified, slice: (start, end) => ({ arrayBuffer: async () => new ArrayBuffer(end - start) }) };
}

interface Server {
	deps: QueueDeps;
	puts: { id: number; n: number }[];
	created: string[];
	completed: number[];
	removed: number[];
	stored: Map<number, Set<number>>;
	storage: Map<string, string>;
	/** Holds chunk replies back until released, to observe what runs at the same time. */
	release: () => void;
	gate: { closed: boolean };
	failNext: { status: number; count: number };
}

function server(): Server {
	const stored = new Map<number, Set<number>>();
	const ids = new Map<string, number>();
	const storage = new Map<string, string>();
	const waiters: (() => void)[] = [];
	const gate = { closed: false };
	const failNext = { status: 0, count: 0 };
	const s: Server = {
		puts: [],
		created: [],
		completed: [],
		removed: [],
		stored,
		storage,
		gate,
		failNext,
		release: () => {
			gate.closed = false;
			for (const w of waiters.splice(0)) w();
		},
		deps: {
			create: async (meta) => {
				s.created.push(meta.filename);
				const id = ids.get(meta.filename) ?? ids.size + 1;
				ids.set(meta.filename, id);
				if (!stored.has(id)) stored.set(id, new Set());
				return { id, chunk_size: CHUNK, received: [...(stored.get(id) as Set<number>)] };
			},
			status: async (id) => ({ received: [...(stored.get(id) ?? [])], chunk_size: CHUNK, status: 'uploading' }),
			putChunk: async (id, n, _body, _hash, signal) => {
				s.puts.push({ id, n });
				if (failNext.count > 0) {
					failNext.count--;
					throw Object.assign(new Error(`HTTP ${failNext.status}`), { status: failNext.status });
				}
				if (gate.closed) {
					await new Promise<void>((resolve, reject) => {
						waiters.push(resolve);
						signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
					});
				}
				stored.get(id)?.add(n);
				return {};
			},
			complete: async (id) => {
				s.completed.push(id);
				return { pst_id: id };
			},
			remove: async (id) => {
				s.removed.push(id);
			},
			hash: async (body) => ({ sha256: sha256Js(body), body }),
			storage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => void storage.set(k, v) },
			now: () => Date.now(),
			sleep: () => tick(),
			onChange: () => {}
		}
	};
	return s;
}

async function until(check: () => boolean, label = 'condition') {
	for (let i = 0; i < 400; i++) {
		if (check()) return;
		await tick(1);
	}
	throw new Error(`timeout waiting for ${label}`);
}

describe('order', () => {
	const files = [file('b.pst', 30, 5), file('a.pst', 10, 9), file('c.pst', 20, 1)];
	it.each([
		['size-asc', ['a.pst', 'c.pst', 'b.pst']],
		['size-desc', ['b.pst', 'c.pst', 'a.pst']],
		['date', ['c.pst', 'b.pst', 'a.pst']],
		['name', ['a.pst', 'b.pst', 'c.pst']]
	] as const)('%s', (order, expected) => {
		expect(sortFiles(files, order).map((f) => f.name)).toEqual(expected);
	});

	it('smallest first by default, whatever the selection order', async () => {
		const s = server();
		const q = new UploadQueue(s.deps);
		q.add(files);
		await until(() => s.completed.length === 3);
		expect(s.created).toEqual(['a.pst', 'c.pst', 'b.pst']);
	});
});

describe('queue', () => {
	it('sends one file at a time by default, with parallel chunks inside it', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst', 60), file('b.pst', 70)]);
		await until(() => s.puts.length === 3, 'three chunks in flight');
		expect(new Set(s.puts.map((p) => p.id)).size).toBe(1);
		expect(q.get('b.pst|70|1')?.status).toBe('queued');
		s.release();
		await until(() => q.get('b.pst|70|1')?.status !== 'queued', 'second file to start');
		expect(q.get('a.pst|60|1')?.status).toBe('done');
		s.release();
		await until(() => s.completed.length === 2);
	});

	it('honours a higher concurrency', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps, 2);
		q.add([file('a.pst', 60), file('b.pst', 70), file('c.pst', 80)]);
		await until(() => s.created.length === 2, 'two files started');
		await tick(5);
		expect(s.created).toEqual(['a.pst', 'b.pst']);
		s.gate.closed = false;
		s.release();
		await until(() => s.completed.length === 3);
	});

	it('raising the concurrency starts the next file at once', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst'), file('b.pst', 40)]);
		await until(() => s.created.length === 1);
		q.setConcurrency(2);
		await until(() => s.created.length === 2);
		s.gate.closed = false;
		s.release();
		await until(() => s.completed.length === 2);
	});

	it('pause then resume sends each chunk at most twice and finishes', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		const { added } = q.add([file('a.pst', 120)]);
		await until(() => s.puts.length >= 3);
		q.pause(added[0].key);
		expect(q.get(added[0].key)?.status).toBe('paused');
		s.gate.closed = false;
		await tick(5);
		q.resume(added[0].key);
		await until(() => q.get(added[0].key)?.status === 'done');
		const counts = new Map<number, number>();
		for (const p of s.puts) counts.set(p.n, (counts.get(p.n) ?? 0) + 1);
		for (const c of counts.values()) expect(c).toBeLessThanOrEqual(2);
		expect(q.get(added[0].key)?.sentBytes).toBe(120);
	});

	it('pausing the active file lets the next one go', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		const { added } = q.add([file('a.pst', 60), file('b.pst', 70)]);
		await until(() => s.puts.length >= 1);
		q.pause(added[0].key);
		await until(() => s.created.includes('b.pst'));
		s.release();
		await until(() => s.completed.length === 1);
		expect(q.get(added[0].key)?.status).toBe('paused');
	});

	it('moves waiting items, never the one being sent', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst', 10), file('b.pst', 20), file('c.pst', 30)]);
		await until(() => s.created.length === 1);
		q.move('c.pst|30|1', -1);
		expect(q.items.map((i) => i.filename)).toEqual(['a.pst', 'c.pst', 'b.pst']);
		q.move('c.pst|30|1', -1); // a.pst is active: c.pst stays second
		expect(q.items.map((i) => i.filename)).toEqual(['a.pst', 'c.pst', 'b.pst']);
		q.move('a.pst|10|1', 1);
		expect(q.items[0].filename).toBe('a.pst');
		s.gate.closed = false;
		s.release();
		await until(() => s.completed.length === 3);
		expect(s.created).toEqual(['a.pst', 'c.pst', 'b.pst']);
	});

	it('moveTo and resort keep active items in place', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst', 10), file('b.pst', 20), file('c.pst', 30), file('d.pst', 40)]);
		await until(() => s.created.length === 1);
		q.moveTo('d.pst|40|1', 'b.pst|20|1');
		expect(q.items.map((i) => i.filename)).toEqual(['a.pst', 'd.pst', 'b.pst', 'c.pst']);
		q.resort('size-asc');
		expect(q.items.map((i) => i.filename)).toEqual(['a.pst', 'b.pst', 'c.pst', 'd.pst']);
		q.resort('size-desc');
		expect(q.items.map((i) => i.filename)).toEqual(['a.pst', 'd.pst', 'c.pst', 'b.pst']);
		s.gate.closed = false;
		s.release();
		await until(() => s.completed.length === 4);
	});

	it('cancel removes the item and frees the server copy', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		const { added } = q.add([file('a.pst', 60)]);
		await until(() => s.puts.length >= 1);
		q.cancel(added[0].key);
		expect(q.items).toHaveLength(0);
		await tick(2);
		expect(s.removed).toEqual([1]);
		expect(JSON.parse(s.storage.get(STORAGE_KEY) as string)).toEqual({});
	});

	it('skips a file already in the queue', async () => {
		const s = server();
		s.gate.closed = true;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst')]);
		const { added, skipped } = q.add([file('a.pst')]);
		expect(added).toHaveLength(0);
		expect(skipped).toEqual(['a.pst']);
		q.cancel('a.pst|30|1');
	});

	it('retries a server error, stops on a fatal one', async () => {
		const s = server();
		s.failNext.status = 503;
		s.failNext.count = 2;
		const q = new UploadQueue(s.deps);
		q.add([file('a.pst', 10)]);
		await until(() => s.completed.length === 1);

		const t = server();
		t.failNext.status = 413;
		t.failNext.count = 1;
		const failed: string[] = [];
		t.deps.onFailed = (item) => failed.push(item.error as string);
		const r = new UploadQueue(t.deps);
		r.add([file('b.pst', 10)]);
		await until(() => failed.length === 1);
		expect(r.get('b.pst|10|1')?.status).toBe('error');
		expect(failed[0]).toBe('HTTP 413');
		r.resume('b.pst|10|1');
		await until(() => t.completed.length === 1);
	});

	it('after a reload, lists what was known and resumes from the server state', async () => {
		const s = server();
		s.gate.closed = true;
		const first = new UploadQueue(s.deps);
		first.add([file('a.pst', 60), file('b.pst', 70)]);
		await until(() => s.puts.length >= 3);
		s.stored.get(1)?.add(0);
		s.stored.get(1)?.add(1);

		// New page: same storage and server, no File objects.
		const reloaded = new UploadQueue(s.deps);
		await reloaded.restore();
		expect(reloaded.items.map((i) => [i.filename, i.status])).toEqual([
			['a.pst', 'needs_file'],
			['b.pst', 'needs_file']
		]);
		expect(reloaded.get('a.pst|60|1')?.sentBytes).toBe(20);

		s.gate.closed = false;
		s.puts.length = 0;
		reloaded.add([file('a.pst', 60)]);
		await until(() => reloaded.get('a.pst|60|1')?.status === 'done');
		expect(s.puts.map((p) => p.n).sort()).toEqual([2, 3, 4, 5]);
		expect(reloaded.get('b.pst|70|1')?.status).toBe('needs_file');
	});

	it('forgets an upload the server no longer has', async () => {
		const s = server();
		s.storage.set(STORAGE_KEY, JSON.stringify({ k: { key: 'k', id: 9, filename: 'gone.pst', size: 10, lastModified: 1 } }));
		s.deps.status = async () => {
			throw Object.assign(new Error('unknown upload'), { status: 404 });
		};
		const q = new UploadQueue(s.deps);
		await q.restore();
		expect(q.items).toHaveLength(0);
	});
});

describe('sha256', () => {
	it('matches the known digest of "abc"', () => {
		const bytes = new TextEncoder().encode('abc');
		expect(sha256Js(bytes.buffer.slice(0) as ArrayBuffer)).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
	});
});
