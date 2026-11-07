/**
 * The one moving surface: the panel slides in. Under prefers-reduced-motion it is just there.
 * Only transform and opacity are animated, so nothing causes layout.
 */
import { cubicOut } from 'svelte/easing';
import type { TransitionConfig } from 'svelte/transition';

export function slide(_node: Element, params: { x?: number; duration?: number } = {}): TransitionConfig {
	const { x = 0, duration = 220 } = params;
	if (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches) return { duration: 0 };
	return {
		duration,
		easing: cubicOut,
		css: (t, u) => `opacity:${t};transform:translateX(${u * x}px)`
	};
}
