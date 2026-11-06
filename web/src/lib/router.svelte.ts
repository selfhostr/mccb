import { legacyRedirect, matchRoute, type Route } from './logic/routes.ts';

class Router {
	path = $state(location.pathname);
	search = $state(location.search);
	route: Route = $derived(matchRoute(this.path));
	params: URLSearchParams = $derived(new URLSearchParams(this.search));

	constructor() {
		const redirect = legacyRedirect(location.pathname, location.hash);
		if (redirect) {
			history.replaceState(null, '', redirect);
			this.sync();
		}
		addEventListener('popstate', () => this.sync());
		// Every internal link goes through here: nothing reloads the page, so uploads survive.
		document.addEventListener('click', (e) => {
			if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
			const link = (e.target as Element | null)?.closest?.('a');
			if (!link || link.target || link.hasAttribute('download')) return;
			const url = new URL(link.href, location.href);
			if (url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
			if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
			e.preventDefault();
			this.go(url.pathname + url.search + url.hash);
		});
	}

	private sync(): void {
		this.path = location.pathname;
		this.search = location.search;
	}

	go(url: string): void {
		const before = this.route.page;
		history.pushState(null, '', url);
		this.sync();
		const hash = new URL(url, location.href).hash;
		if (hash) setTimeout(() => document.getElementById(hash.slice(1))?.scrollIntoView());
		// Opening and closing the job panel keeps the table where it is.
		else if (this.route.page !== before) scrollTo(0, 0);
	}

	/** Filters live in the URL. Replaces, so that typing does not fill the history. */
	setParams(values: Record<string, string | null>): void {
		const next = new URLSearchParams(location.search);
		for (const [key, value] of Object.entries(values)) {
			if (value) next.set(key, value);
			else next.delete(key);
		}
		const text = next.toString();
		try {
			history.replaceState(null, '', `${location.pathname}${text ? `?${text}` : ''}${location.hash}`);
		} catch {
			// Some browsers limit replaceState: the filter still applies, the URL catches up next time.
		}
		this.search = text ? `?${text}` : '';
	}
}

export const router = new Router();
