/** Below this many rows everything is rendered: a window only pays off on long lists. */
export const VIRTUAL_AT = 150;

export interface WindowRange {
	start: number;
	end: number;
	before: number;
	after: number;
}

/** Which rows of a uniform list to render. `offset` is how far the list has scrolled past the top of the viewport. */
export function windowRange(
	count: number,
	offset: number,
	viewport: number,
	rowHeight: number,
	overscan = 12,
	threshold = VIRTUAL_AT
): WindowRange {
	if (count <= threshold || rowHeight <= 0) return { start: 0, end: count, before: 0, after: 0 };
	const first = Math.floor(Math.max(0, offset) / rowHeight);
	const start = Math.min(Math.max(0, first - overscan), Math.max(0, count - 1));
	const end = Math.min(count, first + Math.ceil(viewport / rowHeight) + overscan);
	return { start, end, before: start * rowHeight, after: (count - end) * rowHeight };
}
