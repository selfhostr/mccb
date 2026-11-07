import { expect, ready, test } from './helpers';

test.describe('repair', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('add a mailbox, it is tested, analysed, previewed, then repaired after a confirmation', async ({ page }) => {
		await ready(page, '/repair');
		await expect(page.getByRole('link', { name: 'Repair' })).toHaveAttribute('aria-current', 'page');
		await page.getByTestId('repair-add').first().click();
		await page.getByTestId('repair-box-email').fill('carol@example.com');
		await page.getByTestId('repair-box-password').fill('secret');
		await page.getByTestId('repair-box-save').click();

		const row = page.locator('[data-repair-box="carol@example.com"]');
		await expect(row.getByTestId('repair-conn-state')).toHaveAttribute('data-conn', 'ok');
		await expect(row.getByTestId('repair-state')).toContainText('Not analysed');

		await row.getByRole('button', { name: 'More actions for carol@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Analyse' }).click();
		await expect(row.getByTestId('repair-state')).toContainText('Damaged');
		await expect(row.getByTestId('repair-line')).toContainText('Damaged: 14 of 420 messages');

		await row.getByTestId('repair-open').click();
		const detail = page.getByTestId('repair-detail');
		await expect(detail.getByTestId('repair-issues')).toContainText('Accents replaced');
		await expect(detail.getByTestId('repair-folders')).toContainText('Archive/2023');
		await detail.getByTestId('repair-preview').first().click();
		await expect(page.getByTestId('repair-preview-dialog')).toContainText('offre révisée');
		await page.getByRole('button', { name: 'Close' }).last().click();

		await detail.getByTestId('repair-detail-repair').click();
		await expect(page.getByRole('dialog')).toContainText('_originaux_avant_reparation');
		await page.getByRole('dialog').getByRole('button', { name: 'Repair' }).click();
		await expect(row.getByTestId('repair-state')).toContainText('Repaired');
	});

	test('a wrong password shows at a glance, and the selection runs in bulk', async ({ page }) => {
		await ready(page, '/repair');
		for (const email of ['bad@example.com', 'clean@example.com']) {
			await page.getByTestId('repair-add').first().click();
			await page.getByTestId('repair-box-email').fill(email);
			await page.getByTestId('repair-box-password').fill('x');
			await page.getByTestId('repair-box-save').click();
			await expect(page.locator(`[data-repair-box="${email}"]`)).toBeVisible();
		}
		await expect(page.locator('[data-repair-box="bad@example.com"]').getByTestId('repair-conn-state')).toHaveAttribute('data-conn', 'failed');
		await expect(page.getByTestId('repair-conn')).toContainText('Failed: 1');

		await page.getByRole('checkbox', { name: 'Select all' }).check();
		await page.getByTestId('bulk-scan').click();
		await expect(page.locator('[data-repair-box="clean@example.com"]').getByTestId('repair-state')).toContainText('Intact');
	});
});
