import { del, errorText, get, post, put } from '../api/client.ts';
import type { CheckResult, LinkInput, LinkResponse, MatchResponse, SourceConnection, SourceConnectionInput, TenantMailboxes } from '../api/types.ts';

/** Source connections (Microsoft 365 tenants), as GET /api/source-connections serves them. */
class Sources {
	list = $state.raw<SourceConnection[]>([]);
	loaded = $state(false);
	error = $state<string | null>(null);
	private pending: Promise<void> | undefined;

	load(): Promise<void> {
		this.pending ??= (async () => {
			try {
				const list = await get<SourceConnection[]>('/source-connections');
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

	byId(id: number): SourceConnection | undefined {
		return this.list.find((c) => c.id === id);
	}

	/** Tests a form (unsaved; `id` lets omitted secrets come from the stored connection) or a stored connection. */
	test(input: { kind: string; config: Record<string, unknown>; secrets?: Record<string, string>; id?: number }): Promise<CheckResult> {
		return post<CheckResult>('/source-connections/test', input);
	}

	async testStored(id: number): Promise<CheckResult> {
		const result = await post<CheckResult>(`/source-connections/${id}/test`);
		await this.reload();
		return result;
	}

	async save(input: SourceConnectionInput, id?: number): Promise<SourceConnection> {
		const saved = id == null ? await post<SourceConnection>('/source-connections', input) : await put<SourceConnection>(`/source-connections/${id}`, input);
		await this.reload();
		return saved;
	}

	async remove(id: number): Promise<void> {
		await del(`/source-connections/${id}`);
		await this.reload();
	}

	mailboxes(id: number, refresh = false): Promise<TenantMailboxes> {
		return get<TenantMailboxes>(`/source-connections/${id}/mailboxes?refresh=${refresh ? 1 : 0}`);
	}

	match(id: number, serverId: number, refresh = false): Promise<MatchResponse> {
		return get<MatchResponse>(`/source-connections/${id}/match?server_id=${serverId}&refresh=${refresh ? 1 : 0}`);
	}

	link(id: number, input: LinkInput): Promise<LinkResponse> {
		return post<LinkResponse>(`/source-connections/${id}/link`, input);
	}
}

export const sources = new Sources();
