import { sha256Js, toHex } from './sha256.ts';

/**
 * Hashing must not freeze the page:
 * 1. crypto.subtle when it exists (HTTPS or localhost): native and off the main thread;
 * 2. plain HTTP: the JS implementation in a worker;
 * 3. no worker, or it failed to load: the JS implementation here, as a last resort.
 * The worker gets a copy of the chunk, so a failure there loses nothing.
 */
type Pending = { resolve: (sha256: string) => void; body: ArrayBuffer };

let worker: Worker | null | undefined;
let nextId = 0;
const pending = new Map<number, Pending>();

function getWorker(): Worker | null {
	if (worker !== undefined) return worker;
	try {
		worker = new Worker(new URL('./hash.worker.ts', import.meta.url), { type: 'module' });
		worker.onmessage = (event: MessageEvent<{ id: number; sha256: string }>) => {
			pending.get(event.data.id)?.resolve(event.data.sha256);
			pending.delete(event.data.id);
		};
		worker.onerror = () => {
			worker = null;
			for (const [id, entry] of pending) {
				pending.delete(id);
				entry.resolve(sha256Js(entry.body));
			}
		};
	} catch {
		worker = null;
	}
	return worker;
}

export async function hashChunk(body: ArrayBuffer): Promise<{ sha256: string; body: ArrayBuffer }> {
	if (globalThis.crypto?.subtle) {
		return { sha256: toHex(await crypto.subtle.digest('SHA-256', body)), body };
	}
	const w = getWorker();
	if (!w) return { sha256: sha256Js(body), body };
	const sha256 = await new Promise<string>((resolve) => {
		const id = ++nextId;
		pending.set(id, { resolve, body });
		w.postMessage({ id, body });
	});
	return { sha256, body };
}
