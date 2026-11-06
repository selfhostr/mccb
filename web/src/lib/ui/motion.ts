/**
 * Motion helpers. Under prefers-reduced-motion the state still changes, it just does not
 * move. Only transform and opacity are ever animated, so nothing causes layout.
 */
import { cubicOut } from 'svelte/easing';
import type { EasingFunction, TransitionConfig } from 'svelte/transition';

export function reducedMotion(): boolean {
	return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Slide in from a side or from below, fading. */
export function slide(_node: Element, params: { x?: number; y?: number; duration?: number; easing?: EasingFunction } = {}): TransitionConfig {
	const { x = 0, y = 0, duration = 220, easing = cubicOut } = params;
	if (reducedMotion()) return { duration: 0 };
	return {
		duration,
		easing,
		css: (t, u) => `opacity:${t};transform:translate(${u * x}px,${u * y}px)`
	};
}

/** Row enter and leave: fade only, the row keeps its place. */
export function rowFade(_node: Element, params: { duration?: number } = {}): TransitionConfig {
	if (reducedMotion()) return { duration: 0 };
	return { duration: params.duration ?? 160, easing: cubicOut, css: (t) => `opacity:${t}` };
}
