export type Page = 'migrations' | 'files' | 'servers' | 'settings';

export interface Route {
	page: Page;
	jobId: number | null;
}

export function matchRoute(path: string): Route {
	const clean = path.replace(/\/+$/, '') || '/';
	const job = /^\/jobs\/(\d+)$/.exec(clean);
	if (job) return { page: 'migrations', jobId: Number(job[1]) };
	if (clean === '/files') return { page: 'files', jobId: null };
	if (clean === '/servers') return { page: 'servers', jobId: null };
	if (clean === '/settings') return { page: 'settings', jobId: null };
	return { page: 'migrations', jobId: null };
}

/**
 * Where a row of the list leads, keeping the filters: the job panel once there is a job, else the
 * same panel with only the mailbox settings. The panel's own view does not follow to the next row.
 */
export function rowHref(search: string, accountId: number, jobId: number | null | undefined): string {
	const params = new URLSearchParams(search);
	params.delete('tab');
	params.delete('account');
	if (jobId == null) {
		params.set('account', String(accountId));
		return `/?${params}`;
	}
	const text = params.toString();
	return `/jobs/${jobId}${text ? `?${text}` : ''}`;
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
			// Now a page of its own: only the old hash form needs to move.
			return hash.startsWith('#/') ? '/servers' : null;
		default:
			break;
	}
	if (hash.startsWith('#/')) return where === '/settings' ? '/settings' : '/';
	return null;
}
