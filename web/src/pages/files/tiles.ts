/**
 * Choice tiles in a role="radiogroup": one tab stop for the group (the checked tile, else the first),
 * the arrow keys move and choose, as with native radio buttons. Shared with the new migration form.
 */

const KEYS = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'];

/** Put on the radiogroup element: `onkeydown={roving}`. */
export function roving(e: KeyboardEvent): void {
	if (!KEYS.includes(e.key)) return;
	const group = e.currentTarget as HTMLElement;
	const radios = [...group.querySelectorAll<HTMLButtonElement>('[role="radio"]:not(:disabled)')];
	if (!radios.length) return;
	e.preventDefault();
	const at = radios.indexOf(document.activeElement as HTMLButtonElement);
	const rtl = getComputedStyle(group).direction === 'rtl';
	let next: number;
	if (e.key === 'Home') next = 0;
	else if (e.key === 'End') next = radios.length - 1;
	else {
		const forward = e.key === 'ArrowDown' || e.key === (rtl ? 'ArrowLeft' : 'ArrowRight');
		next = ((at < 0 ? 0 : at) + (forward ? 1 : -1) + radios.length) % radios.length;
	}
	radios[next].focus();
	radios[next].click();
}

/** The tab stop: the checked tile, or the first one when none is checked. */
export const tabStop = (checked: boolean, index: number, anyChecked: boolean): 0 | -1 => (checked || (!anyChecked && index === 0) ? 0 : -1);

/** A checked tile reads at once, in both themes: a strong ring and a check mark (drawn by the tile). */
export const tileClass = (selected: boolean): string =>
	`relative flex min-h-12 min-w-0 items-center gap-2 rounded-btn border ps-2 pe-6 text-left transition-[border-color,box-shadow,color] duration-200 disabled:cursor-not-allowed disabled:opacity-50 ${selected ? 'border-ink bg-surface-2 text-ink ring-2 ring-ink' : 'border-line text-ink-2 hover:border-line-strong hover:text-ink'}`;
