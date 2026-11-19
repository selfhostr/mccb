export type ToastTone = 'signal' | 'info' | 'advisory' | 'warning';

export interface ToastEntry {
	id: number;
	message: string;
	tone: ToastTone;
}

/** How long each tone stays when the caller gives no duration: a problem stays long enough to be read. */
const DEFAULT_MS: Record<ToastTone, number> = { signal: 4500, info: 6000, advisory: 9000, warning: 12000 };

class Toasts {
	list = $state.raw<ToastEntry[]>([]);
	private next = 0;

	/** `ms` overrides the tone's duration; `0` keeps the toast until it is closed. */
	push(message: string, tone: ToastTone = 'info', ms: number = DEFAULT_MS[tone]): void {
		const id = ++this.next;
		this.list = [...this.list, { id, message, tone }].slice(-4);
		if (ms) setTimeout(() => this.dismiss(id), ms);
	}

	dismiss(id: number): void {
		this.list = this.list.filter((t) => t.id !== id);
	}
}

export const toasts = new Toasts();
export const toast = (message: string, tone?: ToastTone, ms?: number) => toasts.push(message, tone, ms);
