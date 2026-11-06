export type ToastTone = 'signal' | 'info' | 'advisory' | 'warning';

export interface ToastEntry {
	id: number;
	message: string;
	tone: ToastTone;
}

class Toasts {
	list = $state.raw<ToastEntry[]>([]);
	private next = 0;

	push(message: string, tone: ToastTone = 'info', ms = 4500): void {
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
