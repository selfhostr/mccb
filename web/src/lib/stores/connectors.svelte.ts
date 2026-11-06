import { errorText, get } from '../api/client.ts';
import type { Connector } from '../api/types.ts';

/**
 * The connector catalogue, as GET /api/connectors serves it: a plain array of
 * { kind, label, description, source, target }. Loaded once; forms are drawn from it.
 * A server without the route leaves it empty and the UI keeps to PSTs.
 */
class Connectors {
	list = $state.raw<Connector[]>([]);
	loaded = $state(false);
	error = $state<string | null>(null);
	private pending: Promise<void> | undefined;

	load(): Promise<void> {
		this.pending ??= (async () => {
			try {
				const list = await get<Connector[]>('/connectors');
				this.list = Array.isArray(list) ? list : [];
				this.error = null;
			} catch (error) {
				this.list = [];
				this.error = errorText(error);
			} finally {
				this.loaded = true;
			}
		})();
		return this.pending;
	}

	reload(): Promise<void> {
		this.pending = undefined;
		return this.load();
	}
}

export const connectors = new Connectors();
