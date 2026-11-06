import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { m } from '#lib/paraglide/messages.js';
import { errorText, get, post, put } from '../api/client.ts';
import { type LiveEvent, startEvents, stopEvents } from '../api/events.ts';
import type { Account, AccountOptions, Health, Job, Pst, Server, TestResult } from '../api/types.ts';
import { testPassed } from '../logic/accounts.ts';
import { createTestRunner, type TestPhase } from '../logic/testrun.ts';
import { toast } from '../ui/toast.svelte.ts';
import { uploads } from '../upload/store.svelte.ts';
import { connectors } from './connectors.svelte.ts';

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * All server state. Jobs are kept apart from accounts: a progress event touches one entry
 * of one map, so one row reacts and the others do not even run.
 */
class Data {
	servers = $state.raw<Server[]>([]);
	accounts = new SvelteMap<number, Account>();
	jobs = new SvelteMap<number, Job>();
	psts = new SvelteMap<number, Pst>();
	testing = new SvelteMap<number, TestPhase>();
	/** Survives a change of page; hidden rows stay counted, see logic/selection.ts. */
	selected = new SvelteSet<number>();
	loading = $state(true);
	error = $state<string | null>(null);
	live = $state(false);
	workerAlive = $state(true);
	version = $state('');
	private healthTimer: ReturnType<typeof setInterval> | undefined;
	/** Toggles answer at once: while a write is on its way, an older echo from the stream must not flip them back. */
	private optionWrites = new Map<number, number>();

	private tests = createTestRunner<TestResult>({
		run: async (id) => {
			const result = await post<TestResult>(`/accounts/${id}/test`);
			// Status, time and result come back as the server stored them.
			this.upsertAccount(await get<Account>(`/accounts/${id}`));
			return result;
		},
		onPhase: (id, phase) => {
			if (phase) this.testing.set(id, phase);
			else this.testing.delete(id);
		},
		onBatchDone: ({ total, results }) => {
			let failed = 0;
			for (const r of results.values()) if (r instanceof Error || !testPassed(r)) failed++;
			toast(m.migrations_test_done({ total, failed }), failed ? 'advisory' : 'signal');
		}
	});

	test = (ids: readonly number[]) => this.tests.enqueue(ids);

	async start(): Promise<void> {
		await this.load();
		startEvents({
			onBatch: (events) => this.apply(events),
			onState: (connected) => (this.live = connected),
			onReconnect: () => void this.load()
		});
		void this.checkHealth();
		void connectors.load();
		clearInterval(this.healthTimer);
		this.healthTimer = setInterval(() => void this.checkHealth(), 30000);
	}

	stop(): void {
		stopEvents();
		clearInterval(this.healthTimer);
		this.live = false;
	}

	async load(): Promise<void> {
		this.error = null;
		try {
			const [servers, accounts, psts] = await Promise.all([get<Server[]>('/servers'), get<Account[]>('/accounts'), get<Pst[]>('/psts')]);
			this.servers = servers;
			const ids = new Set(accounts.map((a) => a.id));
			for (const id of [...this.accounts.keys()]) if (!ids.has(id)) this.removeAccount(id);
			for (const account of accounts) this.upsertAccount(account);
			const pstIds = new Set(psts.map((p) => p.id));
			for (const id of [...this.psts.keys()]) if (!pstIds.has(id)) this.psts.delete(id);
			for (const pst of psts) this.upsertPst(pst);
		} catch (error) {
			this.error = errorText(error);
		} finally {
			this.loading = false;
		}
	}

	async reloadServers(): Promise<void> {
		this.servers = await get<Server[]>('/servers');
	}

	private async checkHealth(): Promise<void> {
		try {
			const health = await get<Health>('/health', { noAuthRedirect: true });
			this.workerAlive = health.worker?.alive ?? true;
			this.version = health.version ?? this.version;
		} catch {
			// The stream state already tells that the server is away.
		}
	}

	upsertAccount(account: Account): void {
		const { job, ...rest } = account;
		const current = this.accounts.get(account.id);
		const next = { ...rest, job: null, options: current && this.optionWrites.get(account.id) ? current.options : rest.options } as Account;
		if (!same(current, next)) this.accounts.set(account.id, next);
		if (job) this.upsertJob(job);
	}

	private removeAccount(id: number): void {
		this.accounts.delete(id);
		this.jobs.delete(id);
		this.selected.delete(id);
	}

	upsertJob(job: Job): void {
		const current = this.jobs.get(job.account_id);
		if (current && current.id > job.id) return;
		this.jobs.set(job.account_id, job);
	}

	upsertPst(pst: Pst): void {
		if (same(this.psts.get(pst.id), pst)) return;
		this.psts.set(pst.id, pst);
		if (pst.status === 'valid') {
			const account = uploads.takeAssignment(pst.id);
			if (account != null) void this.setPst(account, pst.id);
		}
	}

	private apply(events: LiveEvent[]): void {
		for (const { kind, data } of events) {
			if (kind === 'job') {
				if (!data.deleted) this.upsertJob(data as unknown as Job);
			} else if (kind === 'account') {
				if (data.deleted) this.removeAccount(data.id);
				else this.upsertAccount(data as unknown as Account);
			} else if (data.deleted) {
				this.psts.delete(data.id);
			} else {
				this.upsertPst(data as unknown as Pst);
			}
		}
	}

	async setOption(id: number, key: keyof AccountOptions, value: boolean): Promise<void> {
		const before = this.accounts.get(id);
		if (!before) return;
		const writes = (this.optionWrites.get(id) ?? 0) + 1;
		this.optionWrites.set(id, writes);
		this.accounts.set(id, { ...before, options: { ...before.options, [key]: value } });
		try {
			await put(`/accounts/${id}`, { options: { [key]: value } });
		} catch (error) {
			const current = this.accounts.get(id);
			if (current) this.accounts.set(id, { ...current, options: { ...current.options, [key]: before.options[key] } });
			toast(errorText(error), 'warning');
		} finally {
			this.optionWrites.set(id, (this.optionWrites.get(id) ?? 1) - 1);
		}
	}

	async setPst(accountId: number, pstId: number | null): Promise<void> {
		try {
			this.upsertAccount(await put<Account>(`/accounts/${accountId}/pst`, { pst_id: pstId }));
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}

	/** Stops a download. The row goes away at once when the server removed it, else it shows "cancelling". */
	async cancelFetch(id: number): Promise<void> {
		try {
			const { state } = await post<{ ok: boolean; state: string }>(`/psts/${id}/cancel`);
			const current = this.psts.get(id);
			if (state === 'removed') this.psts.delete(id);
			else if (current) this.psts.set(id, { ...current, status: 'cancelling' });
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}

	async retryFetch(id: number): Promise<void> {
		try {
			this.upsertPst(await post<Pst>(`/psts/${id}/retry-fetch`));
		} catch (error) {
			toast(errorText(error), 'warning');
		}
	}
}

export const data = new Data();
