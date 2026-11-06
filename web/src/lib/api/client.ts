export class ApiError extends Error {
	status: number;
	body: unknown;
	constructor(status: number, message: string, body: unknown) {
		super(message);
		this.status = status;
		this.body = body;
	}
}

export interface RequestOptions {
	signal?: AbortSignal;
	headers?: Record<string, string>;
	/** Do not sign out on a 401: the login form and the password form handle it themselves. */
	noAuthRedirect?: boolean;
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void): void {
	onUnauthorized = handler;
}

function detailOf(body: unknown, fallback: string): string {
	if (body && typeof body === 'object') {
		const b = body as Record<string, unknown>;
		const d = b.detail ?? b.message ?? b.error;
		if (typeof d === 'string') return d;
		if (d && typeof d === 'object' && typeof (d as { msg?: unknown }).msg === 'string') return (d as { msg: string }).msg;
		if (Array.isArray(d) && d[0] && typeof d[0].msg === 'string') return d[0].msg;
		if (d) return JSON.stringify(d);
	}
	return fallback;
}

export async function request<T = unknown>(method: string, path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
	const init: RequestInit = { method, credentials: 'same-origin', headers: { ...options.headers }, signal: options.signal };
	if (body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer) {
		init.body = body;
	} else if (body !== undefined) {
		init.body = JSON.stringify(body);
		(init.headers as Record<string, string>)['Content-Type'] = 'application/json';
	}
	const res = await fetch(`/api${path}`, init);
	if (res.status === 401 && !options.noAuthRedirect) onUnauthorized();
	if (!res.ok) {
		let parsed: unknown = null;
		try {
			parsed = await res.json();
		} catch {
			// Not JSON: the status text is all there is.
		}
		throw new ApiError(res.status, detailOf(parsed, res.statusText || `HTTP ${res.status}`), parsed);
	}
	const type = res.headers.get('content-type') ?? '';
	return (type.includes('json') ? await res.json() : await res.text()) as T;
}

export const get = <T>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options);
export const post = <T>(path: string, body: unknown = {}, options?: RequestOptions) => request<T>('POST', path, body, options);
export const put = <T>(path: string, body: unknown, options?: RequestOptions) => request<T>('PUT', path, body, options);
export const del = <T>(path: string, options?: RequestOptions) => request<T>('DELETE', path, undefined, options);

export function errorText(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
