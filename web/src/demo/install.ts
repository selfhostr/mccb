/**
 * The static demo: the real UI with the API simulated in the visitor's browser. Loaded only by the build of
 * the demo (see main.ts). It replaces `fetch` and `EventSource` for /api, so that no request leaves the page:
 * no service worker, no WebSocket, no other origin. State lives in memory and is forgotten on reload.
 */
import type { Component } from 'svelte';
import DemoBadge from './DemoBadge.svelte';
import { handle, hooks, reset, subscribe } from './mock-core.mjs';

interface Reply {
	code: number;
	body: unknown;
	headers: Record<string, string>;
}

const NO_BODY = new Set([101, 204, 205, 304]);

function toResponse({ code, body, headers }: Reply): Response {
	const text = typeof body === 'string' ? body : JSON.stringify(body);
	const type = typeof body === 'string' ? 'text/plain' : 'application/json';
	return new Response(NO_BODY.has(code) ? null : text, { status: code, headers: { 'Content-Type': type, ...headers } });
}

/** One request of /api, answered by the simulated API. */
async function answer(request: Request): Promise<Response> {
	const url = new URL(request.url);
	const path = url.pathname.slice('/api'.length);
	// Anyone signs in: there is no password to guess.
	if (path === '/login') return toResponse({ code: 200, body: { ok: true, authenticated: true }, headers: {} });
	if (path === '/logout') return toResponse({ code: 200, body: { ok: true }, headers: {} });
	const reply = (await handle({
		method: request.method,
		path,
		query: url.searchParams,
		headers: Object.fromEntries(request.headers),
		text: () => request.text(),
		bytes: async () => new Uint8Array(await request.arrayBuffer())
	})) as Reply;
	// A short pause, as a real server has: spinners and "running" states stay visible.
	await new Promise((resolve) => setTimeout(resolve, 25 + Math.random() * 50));
	return toResponse(reply);
}

function installFetch(): void {
	const real = window.fetch.bind(window);
	window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const request = input instanceof Request && !init ? input : new Request(input instanceof Request ? input.url : String(input), init);
		const url = new URL(request.url);
		// Nothing leaves the page: another origin is refused, as a network failure would.
		if (url.origin !== location.origin) throw new TypeError('Network access is disabled in the demo');
		if (!url.pathname.startsWith('/api/')) return real(request);
		if (request.signal.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
		return answer(request);
	};
}

/** The event stream of the UI, fed by the simulated API instead of the network. */
class DemoEventSource extends EventTarget {
	static readonly CONNECTING = 0;
	static readonly OPEN = 1;
	static readonly CLOSED = 2;
	readonly url: string;
	readyState = 0;
	onopen: ((e: Event) => void) | null = null;
	onerror: ((e: Event) => void) | null = null;
	onmessage: ((e: MessageEvent) => void) | null = null;
	private stop: (() => void) | null = null;

	constructor(url: string | URL) {
		super();
		this.url = String(url);
		setTimeout(() => {
			if (this.readyState !== 0) return;
			this.readyState = 1;
			const open = new Event('open');
			this.onopen?.(open);
			this.dispatchEvent(open);
			this.stop = subscribe((kind: string, data: unknown) => this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(data) })));
		}, 30);
	}

	close(): void {
		this.readyState = 2;
		this.stop?.();
		this.stop = null;
	}
}

/**
 * A link to /api (the CSV template, the report) would navigate to the server: the file is made here instead
 * and handed to the browser as a download.
 */
function installDownloads(): void {
	document.addEventListener(
		'click',
		(event) => {
			const link = (event.target as Element | null)?.closest?.('a');
			if (!link || !link.hasAttribute('download')) return;
			const url = new URL(link.href, location.href);
			if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return;
			event.preventDefault();
			event.stopImmediatePropagation();
			void answer(new Request(url)).then(async (response) => {
				const blobUrl = URL.createObjectURL(await response.blob());
				const file = document.createElement('a');
				file.href = blobUrl;
				file.download = link.getAttribute('download') || 'file';
				file.click();
				setTimeout(() => URL.revokeObjectURL(blobUrl), 10_000);
			});
		},
		true
	);
}

export function installDemo(): Component<{ part: 'badge' | 'footer' }> {
	hooks.version = 'demo';
	reset({ demo: true });
	installFetch();
	window.EventSource = DemoEventSource as unknown as typeof EventSource;
	installDownloads();
	return DemoBadge;
}
