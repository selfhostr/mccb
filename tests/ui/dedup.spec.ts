import { expect, noOverflow, ready, test } from './helpers';

const alice = (page: import('./helpers').Page) => page.locator('[data-dedup-box="alice@example.com"]');

test.describe('duplicates', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('analyse, read the detail, then remove after a confirmation that states the counts', async ({ page }) => {
		await ready(page, '/duplicates');
		// Repair and Duplicates are two tabs of one Maintenance page, one sidebar entry.
		await expect(page.getByRole('link', { name: 'Maintenance' })).toHaveAttribute('aria-current', 'page');
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Maintenance');
		await expect(page.getByTestId('maintenance-tabs').getByRole('tab', { name: 'Duplicates' })).toHaveAttribute('aria-selected', 'true');
		const row = alice(page);
		await expect(row.getByTestId('dedup-state')).toContainText('Not analysed');

		// Removal is not offered before an analysis.
		await row.getByRole('button', { name: 'More actions for alice@example.com' }).click();
		await expect(page.getByRole('menuitem', { name: 'Remove duplicates' })).toBeDisabled();
		await page.getByRole('menuitem', { name: 'Analyse' }).click();
		await expect(row.getByTestId('dedup-state')).toContainText('Duplicates found');
		await expect(row.getByTestId('dedup-line')).toContainText('653 duplicates in 433 groups');

		await row.getByTestId('dedup-open').click();
		const detail = page.getByTestId('dedup-detail');
		await expect(detail.getByTestId('dedup-folders')).toContainText('Archive/2023');
		await expect(detail.getByTestId('dedup-refused')).toContainText('UIDPLUS');
		await expect(detail.getByTestId('dedup-examples')).toContainText('Facture mars 2024');
		await expect(detail.getByTestId('dedup-history')).toContainText('Analysis');

		await detail.getByTestId('dedup-detail-remove').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('dedup-confirm-counts')).toContainText('604 messages, 37 events, 12 contacts in 1 mailboxes');
		await expect(dialog).toContainText('PERMANENT');
		await expect(dialog).toContainText('One copy of each item is always kept');
		await dialog.getByTestId('dedup-confirm-go').click();

		await expect(row.getByTestId('dedup-state')).toContainText('Duplicates removed');
		await expect(row.getByTestId('dedup-line')).toContainText('653 removed');
		await expect(detail.getByTestId('dedup-totals')).toContainText('Removed');
		await expect(detail.getByTestId('dedup-history')).toContainText('Removal');
	});

	test('the type filter narrows the confirmation, and bulk removal skips boxes never analysed', async ({ page }) => {
		await ready(page, '/duplicates');
		await page.getByTestId('dedup-kind-calendar').uncheck();
		await page.getByTestId('dedup-kind-contacts').uncheck();
		await expect(page.getByTestId('dedup-kind-mail')).toBeDisabled();

		await alice(page).getByRole('checkbox').check();
		await page.locator('[data-dedup-box="carol@example.com"]').getByRole('checkbox').check();
		await page.getByTestId('bulk-scan').click();
		await expect(alice(page).getByTestId('dedup-state')).toContainText('Duplicates found');
		await page.getByTestId('dedup-bulk').getByRole('button', { name: 'Clear the selection' }).click();

		await alice(page).getByRole('checkbox').check();
		await page.locator('[data-dedup-box="bob@example.com"]').getByRole('checkbox').check();
		await page.getByTestId('bulk-remove').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('dedup-confirm-counts')).toHaveText('604 messages in 1 mailboxes');
		await expect(dialog).toContainText('never analysed');
		await dialog.getByRole('button', { name: 'Cancel' }).click();
	});
});

test.describe('duplicates at 390 px', () => {
	test.use({ viewport: { width: 390, height: 844 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('no horizontal overflow, with the detail and with the dialog open', async ({ page }) => {
		await ready(page, '/duplicates');
		await noOverflow(page);
		const row = alice(page);
		await row.getByRole('button', { name: 'More actions for alice@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Analyse' }).click();
		await expect(row.getByTestId('dedup-state')).toContainText('Duplicates found');
		await row.getByTestId('dedup-open').click();
		await expect(page.getByTestId('dedup-detail').getByTestId('dedup-folders')).toBeVisible();
		await noOverflow(page);
		await page.getByTestId('dedup-detail-remove').click();
		await expect(page.getByRole('dialog')).toBeVisible();
		await noOverflow(page);
	});
});
