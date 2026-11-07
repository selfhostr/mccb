import { closePanel, expect, mock, openMailbox, ready, row, test } from './helpers';

test.use({ viewport: { width: 1280, height: 900 } });

const state = (page: import('./helpers').Page) => mock(page.request, 'state');

async function openRoute(page: import('./helpers').Page, email: string) {
	await row(page, email).getByRole('button', { name: `More actions for ${email}` }).click();
	await page.getByRole('menuitem', { name: 'Source and target' }).click();
	await expect(page.getByRole('dialog')).toContainText(`Source and target of ${email}`);
	return page.getByRole('dialog');
}

test.describe('connectors', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('a PST account stays as simple as before', async ({ page }) => {
		await ready(page);
		const settings = await openMailbox(page, 'alice@example.com');
		await expect(settings.getByRole('combobox', { name: 'PST file for alice@example.com' })).toBeVisible();
		await expect(settings.getByTestId('source-summary')).toHaveCount(0);
		await expect(settings.getByTestId('target-summary')).toHaveCount(0);
		await closePanel(page);
		// The route lives in the row menu, not on the page.
		const dialog = await openRoute(page, 'alice@example.com');
		await expect(dialog.getByTestId('route-source-kind')).toHaveValue('');
		await expect(dialog.getByTestId('route-source-kind').locator('option')).toHaveText(['PST file', 'Exchange (EWS)', 'Future mail service', 'IMAP']);
		await expect(dialog.getByTestId('connector-fields')).toHaveCount(0);
		// Nothing touched: saving sends nothing.
		await page.getByTestId('route-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).routeCalls).toEqual([]);
	});

	test('IMAP source: fields from the catalogue, test, save, secrets kept', async ({ page }) => {
		await ready(page);
		const dialog = await openRoute(page, 'alice@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('imap');
		const fields = dialog.getByTestId('route-source').locator('[data-field]');
		await expect(fields).toHaveCount(5);
		await expect(dialog.getByTestId('route-source').locator('[data-field="port"]')).toHaveValue('993');
		await expect(dialog.getByTestId('route-source').locator('[data-field="security"]')).toHaveValue('ssl');

		// A required field left empty is said, not sent.
		await page.getByTestId('route-save').click();
		await expect(dialog.getByTestId('route-source-problem')).toContainText('Fill in: Server, User, Password.');
		expect((await state(page)).routeCalls).toEqual([]);

		const src = dialog.getByTestId('route-source');
		await src.locator('[data-field="host"]').fill('bad.example.com');
		await src.locator('[data-field="username"]').fill('alice');
		await src.locator('[data-field="password"]').fill('pw');
		await dialog.getByTestId('route-source-test').click();
		await expect(dialog.getByTestId('route-source-result')).toContainText('Unreachable');
		await expect(dialog).toContainText('cannot connect to bad.example.com');

		await src.locator('[data-field="host"]').fill('wrong.example.com');
		await dialog.getByTestId('route-source-test').click();
		await expect(dialog.getByTestId('route-source-result')).toContainText('Credentials refused');

		await src.locator('[data-field="host"]').fill('mail.old.example.com');
		await dialog.getByTestId('route-source-test').click();
		await expect(dialog.getByTestId('route-source-result')).toContainText('Connected');
		await expect(dialog).toContainText('3 folder(s) found');
		const probe = (await state(page)).probeCalls.at(-1);
		expect(probe).toMatchObject({ kind: 'imap', body: { role: 'source', account_id: 1, config: { host: 'mail.old.example.com', port: 993, security: 'ssl', username: 'alice' }, secrets: { password: 'pw' } } });

		await page.getByTestId('route-save').click();
		await expect(page.getByTestId('toast').first()).toContainText('Source and target saved');
		await expect(row(page, 'alice@example.com')).toContainText('IMAP to');
		const alice = await openMailbox(page, 'alice@example.com');
		await expect(alice.getByTestId('source-summary')).toContainText('Source: IMAP');
		await expect(alice.getByTestId('source-summary')).toContainText('mail.old.example.com');
		await expect(alice.getByRole('combobox', { name: /^PST file for/ })).toHaveCount(0);
		const saved = (await state(page)).routeCalls;
		expect(saved).toHaveLength(1);
		expect(saved[0].body).toEqual({ kind: 'imap', config: { host: 'mail.old.example.com', port: 993, security: 'ssl', username: 'alice' }, secrets: { password: 'pw' } });

		// Open again: values are back, the password is "saved", and a change to the host alone keeps it.
		await alice.getByTestId('source-summary').click();
		const again = page.getByRole('dialog');
		await expect(again.locator('[data-field="host"]')).toHaveValue('mail.old.example.com');
		await expect(again.locator('[data-field="password"]')).toHaveAttribute('placeholder', 'Saved. Type to replace it.');
		await again.locator('[data-field="host"]').fill('mail.new.example.com');
		await page.getByTestId('route-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		const last = (await state(page)).routeCalls.at(-1);
		expect('secrets' in last.body).toBe(false);
		expect(last.body.config.host).toBe('mail.new.example.com');
		const acct = (await state(page)).accounts.find((a: { id: number }) => a.id === 1);
		expect(acct.source.has_secret).toBe(true);
		expect(JSON.stringify(acct)).not.toContain('"pw"');
	});

	test('a kind the UI has never seen is drawn from the catalogue, odd field types included', async ({ page }) => {
		await ready(page);
		const dialog = await openRoute(page, 'bob@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('future');
		const src = dialog.getByTestId('route-source');
		await expect(src.locator('[data-field="mailbox"]')).toBeVisible();
		await expect(src.locator('[data-field="mode"]').locator('option')).toHaveText(['fast', 'careful']);
		await expect(src.locator('[data-field="batch"]')).toHaveValue('50');
		// Unknown type "hologram": a plain text field.
		await expect(src.locator('[data-field="weird"]')).toHaveAttribute('type', 'text');
		await expect(dialog).toContainText('Strange field');
		await src.locator('[data-field="mailbox"]').fill('bob');
		await src.locator('[data-field="api_key"]').fill('k-123');
		await src.locator('[data-field="mode"]').selectOption('careful');
		await page.getByTestId('route-save').click();
		await expect((await openMailbox(page, 'bob@example.com')).getByTestId('source-summary')).toContainText('Source: Future mail service');
		expect((await state(page)).routeCalls[0].body).toEqual({ kind: 'future', config: { mailbox: 'bob', mode: 'careful', batch: 50 }, secrets: { api_key: 'k-123' } });
	});

	test('a boolean field and help text from the catalogue', async ({ page }) => {
		await ready(page);
		const dialog = await openRoute(page, 'carol@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('ews');
		await expect(dialog).toContainText('Usually https://mail.example.com/EWS/Exchange.asmx');
		const verify = dialog.getByRole('checkbox', { name: 'Verify the certificate' });
		await expect(verify).toBeChecked();
		await verify.uncheck();
		await dialog.getByTestId('route-source').locator('[data-field="server"]').fill('https://mail.example.com/EWS/Exchange.asmx');
		await dialog.getByTestId('route-source').locator('[data-field="password"]').fill('pw');
		await page.getByTestId('route-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).routeCalls[0].body.config).toEqual({ server: 'https://mail.example.com/EWS/Exchange.asmx', verify_tls: false });
	});

	test('the target sits behind a disclosure, warns about resending, and can go back to the server', async ({ page }) => {
		await ready(page);
		const dialog = await openRoute(page, 'alice@example.com');
		const target = dialog.getByTestId('route-target');
		await expect(target.getByTestId('route-target-kind')).toBeHidden();
		await dialog.getByTestId('route-target-disclosure').locator('summary').click();
		await expect(dialog.getByTestId('route-resend')).toHaveCount(0);
		await expect(target.getByTestId('route-target-kind').locator('option')).toHaveText(["The mailbox's server", 'CalDAV', 'IMAP']);
		await target.getByTestId('route-target-kind').selectOption('imap');
		await expect(dialog.getByTestId('route-resend')).toContainText('everything will be sent again');
		await target.locator('[data-field="host"]').fill('mail.new.example.com');
		await target.locator('[data-field="username"]').fill('alice');
		await target.locator('[data-field="password"]').fill('pw');
		await dialog.getByTestId('route-target-test').click();
		await expect(dialog.getByTestId('route-target-result')).toContainText('Connected');
		await page.getByTestId('route-save').click();
		const settings = await openMailbox(page, 'alice@example.com');
		await expect(settings.getByTestId('target-summary')).toHaveText('Target: IMAP');
		// The PST stays the source.
		await expect(settings.getByRole('combobox', { name: /^PST file for/ })).toBeVisible();
		expect((await state(page)).routeCalls.map((c: { role: string }) => c.role)).toEqual(['target']);

		// Back to the account's server, from the panel this time.
		await settings.getByTestId('account-route').click();
		const again = page.getByRole('dialog');
		await expect(again.getByTestId('route-target-kind')).toHaveValue('imap');
		await again.getByTestId('route-target-kind').selectOption('');
		await page.getByTestId('route-save').click();
		await expect(settings.getByTestId('target-summary')).toHaveCount(0);
		expect((await state(page)).routeCalls.at(-1).body).toEqual({ kind: null });
	});

	test('going back to the PST, and a refused change is explained', async ({ page }) => {
		// Jobs stay queued, so the import below is still active when the change is refused.
		await mock(page.request, 'reset', { jobTick: 600_000 });
		await ready(page);
		let dialog = await openRoute(page, 'alice@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('future');
		await dialog.getByTestId('route-source').locator('[data-field="mailbox"]').fill('alice');
		await dialog.getByTestId('route-source').locator('[data-field="api_key"]').fill('k');
		await page.getByTestId('route-save').click();
		const settings = await openMailbox(page, 'alice@example.com');
		await expect(settings.getByTestId('source-summary')).toBeVisible();
		await closePanel(page);

		dialog = await openRoute(page, 'alice@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('');
		await page.getByTestId('route-save').click();
		await expect((await openMailbox(page, 'alice@example.com')).getByRole('combobox', { name: /^PST file for/ })).toBeVisible();
		await closePanel(page);
		expect((await state(page)).routeCalls.at(-1).body).toEqual({ kind: 'pst' });

		// A running import blocks the change; the server says so.
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await expect(row(page, 'alice@example.com').locator('[data-job-link]')).toBeVisible();
		dialog = await openRoute(page, 'alice@example.com');
		await dialog.getByTestId('route-source-kind').selectOption('future');
		await dialog.getByTestId('route-source').locator('[data-field="mailbox"]').fill('alice');
		await dialog.getByTestId('route-source').locator('[data-field="api_key"]').fill('k');
		await page.getByTestId('route-save').click();
		await expect(dialog.getByRole('alert')).toContainText('a job is active for this mailbox');
	});

	test('an account that reads from a connector can be imported without a PST', async ({ page }) => {
		await ready(page);
		// frank has no PST at all.
		const dialog = await openRoute(page, 'frank@example.org');
		await dialog.getByTestId('route-source-kind').selectOption('future');
		await dialog.getByTestId('route-source').locator('[data-field="mailbox"]').fill('frank');
		await dialog.getByTestId('route-source').locator('[data-field="api_key"]').fill('k');
		await page.getByTestId('route-save').click();
		await row(page, 'frank@example.org').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await expect.poll(async () => (await state(page)).runCalls).toEqual([[6]]);
	});
});

test.describe('layout fixes', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test.describe('at 390 px', () => {
		test.use({ viewport: { width: 390, height: 844 } });

		test('"Select all shown" is a normal row, not a letter-by-letter column', async ({ page }) => {
			await ready(page);
			const label = page.getByTestId('accounts-head').locator('label.check');
			await expect(label).toContainText('Select all shown');
			const box = await label.boundingBox();
			expect(box?.width ?? 0).toBeGreaterThan(100);
			expect(box?.height ?? 999).toBeLessThan(60);
		});
	});
});
