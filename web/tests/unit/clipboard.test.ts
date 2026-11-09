import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyText } from '../../src/lib/clipboard.ts';

function fakeDom(exec: boolean | Error) {
	const area: Record<string, unknown> = { style: {}, setAttribute() {}, focus() {}, select() {}, setSelectionRange() {}, remove: vi.fn() };
	const body = { append: vi.fn() };
	vi.stubGlobal('document', {
		activeElement: null,
		body,
		createElement: () => area,
		execCommand: vi.fn(() => {
			if (exec instanceof Error) throw exec;
			return exec;
		})
	});
	return { area, body };
}

afterEach(() => vi.unstubAllGlobals());

describe('copyText', () => {
	it('uses the async clipboard in a secure context', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal('window', { isSecureContext: true });
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		expect(await copyText('ABC')).toBe(true);
		expect(writeText).toHaveBeenCalledWith('ABC');
	});

	it('falls back to execCommand over plain HTTP', async () => {
		vi.stubGlobal('window', { isSecureContext: false });
		vi.stubGlobal('navigator', {});
		const { area, body } = fakeDom(true);
		expect(await copyText('ABC')).toBe(true);
		expect(area.value).toBe('ABC');
		expect(body.append).toHaveBeenCalled();
		expect(area.remove).toHaveBeenCalled();
	});

	it('falls back when the async clipboard refuses', async () => {
		vi.stubGlobal('window', { isSecureContext: true });
		vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('no')) } });
		fakeDom(true);
		expect(await copyText('ABC')).toBe(true);
	});

	it('reports failure when both routes fail', async () => {
		vi.stubGlobal('window', { isSecureContext: false });
		vi.stubGlobal('navigator', {});
		fakeDom(false);
		expect(await copyText('ABC')).toBe(false);
		fakeDom(new Error('x'));
		expect(await copyText('ABC')).toBe(false);
	});
});
