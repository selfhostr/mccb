// The static demo (npm run build:demo, then npm run test:demo). Served by serve-demo.mjs with the headers of
// web/demo-headers, so a script, a style or a request the policy forbids fails here and not in front of a visitor.
import { expect, type Page, test } from '../../web/node_modules/@playwright/test';

const ORIGIN = 'http://127.0.0.1:4174';

let outside: string[];
let violations: string[];
let errors: string[];

test.beforeEach(async ({ page }) => {
	outside = [];
	violations = [];
	errors = [];
	// Every request the page makes, whatever starts it. Anything that is not this origin is refused and counted.
	await page.route('**/*', (route) => {
		const url = new URL(route.request().url());
		if (url.protocol.startsWith('http') && url.origin !== ORIGIN) {
			outside.push(route.request().url());
			return route.abort();
		}
		return route.continue();
	});
	page.on('websocket', (socket) => outside.push(`websocket ${socket.url()}`));
	page.on('console', (message) => {
		if (message.type() !== 'error') return;
		// A deep link is a 404 status carrying the app, as the host serves it: the browser may log that page itself.
		const page404 = message.text().includes('status of 404') && !message.location().url.includes('/assets/');
		if (!page404) errors.push(message.text());
	});
	page.on('pageerror', (error) => errors.push(error.message));
	await page.addInitScript(() => {
		const seen: string[] = ((window as unknown as { __csp: string[] }).__csp = []);
		document.addEventListener('securitypolicyviolation', (e) => seen.push(`${e.violatedDirective} ${e.blockedURI}`));
	});
});

test.afterEach(async ({ page }) => {
	violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []).catch(() => []);
	expect(outside, 'requests that left the origin').toEqual([]);
	expect(violations, 'CSP violations').toEqual([]);
	expect(errors, 'console errors').toEqual([]);
});

const open = async (page: Page, path = '/demo/') => {
	await page.goto(path);
	await expect(page.getByTestId('live')).toContainText('Live');
};
const api = <T = unknown>(page: Page, path: string, init?: Record<string, unknown>) =>
	page.evaluate(
		async ([p, i]) => {
			const res = await fetch(`/api${p}`, i as RequestInit);
			return { status: res.status, body: await res.json() };
		},
		[path, init] as const
	) as Promise<{ status: number; body: T }>;
const row = (page: Page, email: string) => page.locator('li.acct-row').filter({ hasText: email });

test('opens signed in, with the badge, the install link and fictional data only', async ({ page }) => {
	await open(page);
	await expect(page.getByTestId('demo-badge')).toContainText('Demo, fictional data');
	await expect(page.getByTestId('demo-install')).toHaveAttribute('href', 'https://mccb.selfhostr.io/docs/install/');
	await expect(row(page, 'amelie.fontaine@example.com')).toBeVisible();
	const text = await page.locator('main').innerText();
	const addresses = text.match(/[\w.+-]+@[\w.-]+/g) ?? [];
	expect(addresses.length).toBeGreaterThan(5);
	// Reserved names only (RFC 2606): example.com/org/net, and the .example domain of the fictional tenant.
	for (const address of addresses) expect(address, 'only reserved example domains').toMatch(/@(example\.(com|org|net)|[\w-]+\.example)$/);
});

test('shows running, finished and needs-attention mailboxes, and a batch', async ({ page }) => {
	await open(page);
	await expect(row(page, 'amelie.fontaine@example.com')).toContainText('Importing');
	await expect(row(page, 'ines.costa@example.com')).toContainText('Done');
	await expect(row(page, 'lucas.meyer@example.com')).toContainText('Done with errors');
	const batches = await api<{ name: string; jobs: number }[]>(page, '/batches');
	expect(batches.body[0].jobs).toBeGreaterThan(3);
});

test('progress moves by itself', async ({ page }) => {
	await open(page);
	const done = async () => (await api<{ progress: { mail: { done: number } } }>(page, '/jobs/1')).body.progress.mail.done;
	const first = await done();
	await expect.poll(done, { timeout: 8000 }).toBeGreaterThan(first);
});

test('deep links load the app through 404.html, and navigation keeps the /demo prefix', async ({ page }) => {
	await open(page, '/demo/files');
	await expect(page.getByTestId('pst-list')).toBeVisible();
	await open(page, '/demo/jobs/4');
	await expect(page.getByTestId('job-panel')).toBeVisible();
	await page.getByTestId('job-close').click();
	await expect(page).toHaveURL(ORIGIN + '/demo/');
	await page.getByRole('navigation').getByRole('link', { name: 'Settings' }).click();
	await expect(page).toHaveURL(ORIGIN + '/demo/settings');
	await page.reload();
	await expect(page.getByTestId('live')).toContainText('Live');
	await expect(page).toHaveURL(ORIGIN + '/demo/settings');
});

test('a write is accepted, and forgotten on reload', async ({ page }) => {
	await open(page);
	const before = (await api<unknown[]>(page, '/servers')).body.length;
	const made = await api(page, '/servers', { method: 'POST', body: JSON.stringify({ name: 'Scratch', imap_host: 'imap.example.net' }) });
	expect(made.status).toBe(201);
	expect((await api<unknown[]>(page, '/servers')).body).toHaveLength(before + 1);
	await page.reload();
	await expect(page.getByTestId('live')).toContainText('Live');
	expect((await api<unknown[]>(page, '/servers')).body).toHaveLength(before);
});

test('an upload is simulated and the file becomes valid', async ({ page }) => {
	await open(page, '/demo/files');
	await page.getByTestId('file-input').setInputFiles({ name: 'visitor.pst', mimeType: 'application/octet-stream', buffer: Buffer.alloc(1_500_000, 7) });
	const item = page.locator('li[data-pst="visitor.pst"]');
	await expect(item).toContainText('Valid', { timeout: 15000 });
});

test('starting an import runs a job', async ({ page }) => {
	await open(page);
	const email = 'ines.costa@example.com';
	await row(page, email).getByRole('checkbox').check();
	await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
	await page.getByTestId('import-confirm').click();
	await expect(row(page, email)).toContainText(/Queued|Importing/);
});

test('login is skipped, and any password signs in again after signing out', async ({ page }) => {
	await open(page);
	await page.getByRole('button', { name: 'Sign out' }).click();
	await page.getByTestId('login-password').fill('whatever');
	await page.keyboard.press('Enter');
	await expect(page.getByTestId('live')).toContainText('Live');
});

test('the badge shows at 390 px without horizontal overflow', async ({ page }) => {
	await page.setViewportSize({ width: 390, height: 844 });
	await open(page);
	await expect(page.getByTestId('demo-badge')).toBeVisible();
	const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
	expect(scroll).toBeLessThanOrEqual(client);
});
