export type Page = 'migrations' | 'files' | 'settings';

export interface Route {
	page: Page;
	jobId: number | null;
}

export function matchRoute(path: string): Route {
	const clean = path.replace(/\/+$/, '') || '/';
	const job = /^\/jobs\/(\d+)$/.exec(clean);
	if (job) return { page: 'migrations', jobId: Number(job[1]) };
	if (clean === '/files') return { page: 'files', jobId: null };
	if (clean === '/settings') return { page: 'settings', jobId: null };
	return { page: 'migrations', jobId: null };
}

/** Links of the previous UI (`#/accounts?server=2`) and their path forms. Returns the new URL or null. */
export function legacyRedirect(path: string, hash: string): string | null {
	const old = hash.startsWith('#/') ? hash.slice(1) : path;
	const [where, query] = old.split('?');
	const search = query ? `?${query}` : '';
	const job = /^\/jobs\/(\d+)$/.exec(where);
	if (hash.startsWith('#/') && job) return `/jobs/${job[1]}`;
	switch (where) {
		case '/dashboard':
			return '/';
		case '/accounts':
			return `/${search}`;
		case '/uploads':
			return '/files';
		case '/servers':
			return '/settings#servers';
		default:
			break;
	}
	if (hash.startsWith('#/')) return where === '/settings' ? '/settings' : '/';
	return null;
}
