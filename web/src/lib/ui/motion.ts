/**
 * Motion that answers something: a panel slides in from its edge, a toast rises into place
 * and fades out. Only transform and opacity move, so nothing causes layout, and under
 * prefers-reduced-motion every transition is instant. The CSS side (page entry, lists,
 * skeletons, the "in progress" pulse) lives in app.css, with the same easing (--ease) and durations.
 */
import { cubicOut } from 'svelte/easing';
import type { TransitionConfig } from 'svelte/transition';

/** Same values as --dur-1 / --dur-2 / --dur-3 in app.css. */
export const DUR = { fast: 120, base: 180, slow: 240 } as const;

const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

/** A panel coming from the edge it stays on. */
export function slide(_node: Element, params: { x?: number; duration?: number } = {}): TransitionConfig {
	const { x = 0, duration = 220 } = params;
	if (reduced()) return { duration: 0 };
	return {
		duration,
		easing: cubicOut,
		css: (t, u) => `opacity:${t};transform:translateX(${u * x}px)`
	};
}

/** Something that appears in place: a short rise and a fade. */
export function rise(_node: Element, params: { y?: number; duration?: number } = {}): TransitionConfig {
	const { y = 6, duration = DUR.base } = params;
	if (reduced()) return { duration: 0 };
	return {
		duration,
		easing: cubicOut,
		css: (t, u) => `opacity:${t};transform:translateY(${u * y}px)`
	};
}

/** Something that goes: a quick fade, a touch smaller, so the eye does not follow it. */
export function leave(_node: Element, params: { duration?: number } = {}): TransitionConfig {
	const { duration = DUR.fast } = params;
	if (reduced()) return { duration: 0 };
	return {
		duration,
		easing: cubicOut,
		css: (t) => `opacity:${t};transform:scale(${0.98 + 0.02 * t})`
	};
}
