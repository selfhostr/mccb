// The Playwright package is installed under web/, which is not an ancestor of tests/ui:
// everything imports it from here, through one relative path.
import { expect, type Page, type APIRequestContext, test as base } from '../../web/node_modules/@playwright/test';

export { expect };
export type { Page };

export const test = base.extend<{ signedIn: void }>({
	signedIn: [
		async ({ page }, use) => {
			await mock(page.request, 'reset');
			const res = await page.request.post('/api/login', { data: { password: 'admin' } });
			expect(res.ok()).toBe(true);
			await use();
		},
		{ auto: false }
	]
});

export async function mock(request: APIRequestContext, action: string, data: Record<string, unknown> = {}) {
	const res = await request.post(`/__mock/${action}`, { data });
	expect(res.ok()).toBe(true);
	return res.json();
}

export const rows = (page: Page) => page.locator('li.acct-row');
export const row = (page: Page, email: string) => rows(page).filter({ hasText: email });

/** Opens a mailbox from its row and gives the part of the panel with its test, source and content. */
export async function openMailbox(page: Page, email: string) {
	await row(page, email).locator('[data-account-link]').click();
	const settings = page.getByTestId('job-panel').getByTestId('account-settings');
	await expect(settings).toBeAttached();
	// In the panel of a job it sits in a disclosure.
	if (await settings.evaluate((el) => el.tagName === 'DETAILS' && !(el as HTMLDetailsElement).open)) await settings.locator('summary').click();
	return settings;
}

export async function closePanel(page: Page) {
	await page.getByTestId('job-close').click();
	await expect(page.getByTestId('job-panel')).toHaveCount(0);
}

/** Picks an action from a row's menu. */
export async function rowAction(page: Page, email: string, action: string | RegExp) {
	await row(page, email).getByRole('button', { name: `More actions for ${email}` }).click();
	await page.getByRole('menuitem', { name: action }).click();
}

export async function ready(page: Page, url = '/') {
	await page.goto(url);
	await expect(page.getByTestId('live')).toContainText('Live');
}

export async function noOverflow(page: Page) {
	const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
	expect(scroll, `page is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
}

export function pstFile(name: string, bytes: number) {
	return { name, mimeType: 'application/octet-stream', buffer: Buffer.alloc(bytes, 7) };
}
