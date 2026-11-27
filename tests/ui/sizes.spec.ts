import { expect, mock, openMailbox, ready, row, test } from './helpers';

test.use({ viewport: { width: 1280, height: 800 } });

test.describe('mailbox sizes', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('estimate from the file, measured source, imported bytes during a job', async ({ page }) => {
		await ready(page);
		// alice's PST stands for her mailbox until a job lists it; dave has no source: no size at all.
		await expect(row(page, 'alice@example.com').getByTestId('row-size')).toContainText('≈');
		await expect(row(page, 'dave@example.com').getByTestId('row-size')).toHaveCount(0);
		await expect(page.getByTestId('page-sentence')).toContainText('imported of ≈');
		await expect(page.getByTestId('page-sentence')).toContainText('size known for 3 of 6 mailboxes');

		// A source that can be measured: "calculating" first, then the value, exact.
		const res = await page.request.put('/api/accounts/4/source', {
			data: { kind: 'imap', config: { host: 'imap.example.net', login: 'dave' }, secrets: { password: 'pw' } }
		});
		expect(res.ok()).toBe(true);
		const dave = row(page, 'dave@example.com').getByTestId('row-size');
		await expect(dave).toContainText('GB');
		await expect(dave).not.toContainText('≈');

		await openMailbox(page, 'dave@example.com');
		const facts = page.getByTestId('job-panel').getByTestId('size-source');
		await expect(facts).toContainText('Mailbox size');
		await expect(facts).toContainText('Measured');
		await facts.getByTestId('size-refresh').click();
		await expect(facts.getByTestId('size-total')).toContainText('GB');

		// During a job: imported / total on the row.
		await mock(page.request, 'reset');
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
		await page.getByTestId('import-confirm').click();
		await expect(row(page, 'alice@example.com').getByTestId('row-size')).toContainText(/ \/ ≈/);
	});

	test('a source test tells the size it measured, the saved source shows it in its row', async ({ page }) => {
		await ready(page);
		await row(page, 'dave@example.com').getByRole('button', { name: 'More actions for dave@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Source and destination' }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('route-source-kind').selectOption('imap');
		const src = dialog.getByTestId('route-source');
		await src.locator('[data-field="host"]').fill('mail.old.example.com');
		await src.locator('[data-field="login"]').fill('dave');
		await src.locator('[data-field="password"]').fill('pw');
		await dialog.getByTestId('route-source-test').click();
		await expect(dialog.getByTestId('route-source-size')).toContainText('Mailbox size: 1.18 MB');
		await page.getByTestId('route-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await expect(row(page, 'dave@example.com').getByTestId('row-size')).toContainText('GB');
	});
});
