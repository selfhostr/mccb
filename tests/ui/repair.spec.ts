import { expect, ready, test } from './helpers';

test.describe('repair', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('add a mailbox, it is tested, analysed, previewed, then repaired after a confirmation', async ({ page }) => {
		await ready(page, '/repair');
		// Repair and Duplicates are two tabs of one Maintenance page, one sidebar entry.
		await expect(page.getByRole('link', { name: 'Maintenance' })).toHaveAttribute('aria-current', 'page');
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Maintenance');
		await expect(page.getByTestId('maintenance-tabs').getByRole('tab', { name: 'Repair' })).toHaveAttribute('aria-selected', 'true');
		await page.getByTestId('repair-add').first().click();
		await page.getByTestId('repair-box-email').fill('carol@example.com');
		await page.getByTestId('repair-box-password').fill('secret');
		await page.getByTestId('repair-box-save').click();

		// The mailbox is tested right after it is saved: the dialog closes when the connection works.
		await expect(page.getByRole('dialog')).toHaveCount(0);
		const row = page.locator('[data-repair-box="carol@example.com"]');
		await expect(row.getByTestId('repair-conn-state')).toHaveAttribute('data-conn', 'ok');
		await expect(row.getByTestId('repair-state')).toContainText('Not analysed');

		// Repair is off until an analysis found damage, and the menu says why.
		await row.getByRole('button', { name: 'More actions for carol@example.com' }).click();
		const repairItem = page.getByRole('menuitem', { name: /^Repair/ });
		await expect(repairItem).toHaveAttribute('aria-disabled', 'true');
		await expect(repairItem).toContainText('Run an analysis that finds damaged messages first');
		await page.getByRole('menuitem', { name: 'Analyse', exact: true }).click();
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
		// The confirmation says how many messages in how many mailboxes, and where the originals go.
		const confirm = page.getByRole('dialog');
		await expect(confirm).toContainText('Repair these mailboxes?');
		await expect(confirm.getByTestId('repair-confirm-counts')).toHaveText('14 messages in 1 mailboxes');
		await expect(confirm).toContainText('_originaux_avant_reparation');
		await confirm.getByRole('button', { name: 'Repair', exact: true }).click();
		await expect(row.getByTestId('repair-state')).toContainText('Repaired');
	});

	test('the next step is on the row itself, and a mailbox leaves the list without being deleted', async ({ page }) => {
		await ready(page, '/repair');
		// Nothing yet: the page offers to add a mailbox, not a button in the header.
		await expect(page.getByTestId('repair-add')).toHaveCount(1);
		await page.getByTestId('repair-add').click();
		await page.getByTestId('repair-box-email').fill('carol@example.com');
		await page.getByTestId('repair-box-password').fill('secret');
		await page.getByTestId('repair-box-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);

		const row = page.locator('[data-repair-box="carol@example.com"]');
		await expect(row.getByTestId('repair-next')).toHaveText('Analyse');
		await row.getByTestId('repair-next').click();
		await expect(row.getByTestId('repair-state')).toContainText('Damaged');
		await expect(row.getByTestId('repair-next')).toHaveText('Repair');

		// "Remove from list" says that nothing changes in the mailbox itself.
		await row.getByRole('button', { name: 'More actions for carol@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Remove from list' }).click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Remove this mailbox from the list?');
		await expect(dialog).toContainText('Nothing changes in the mailbox itself');
		await dialog.getByRole('button', { name: 'Remove', exact: true }).click();
		await expect(page.getByTestId('toast').filter({ hasText: 'carol@example.com removed from the list' })).toBeVisible();
		await expect(row).toHaveCount(0);
	});

	test('a wrong password shows at a glance, and the selection runs in bulk', async ({ page }) => {
		await ready(page, '/repair');
		for (const email of ['bad@example.com', 'clean@example.com']) {
			await page.getByTestId('repair-add').first().click();
			await page.getByTestId('repair-box-email').fill(email);
			await page.getByTestId('repair-box-password').fill('x');
			await page.getByTestId('repair-box-save').click();
			await expect(page.locator(`[data-repair-box="${email}"]`)).toBeVisible();
			if (email.startsWith('bad')) {
				// A refused login keeps the dialog open, with the reason: it has to be closed by hand.
				const dialog = page.getByRole('dialog');
				await expect(dialog).toContainText('Mailbox saved, but the connection failed');
				await expect(dialog).toContainText('LOGIN failed');
				await dialog.getByRole('button', { name: 'Close' }).last().click();
			}
			await expect(page.getByRole('dialog')).toHaveCount(0);
		}
		await expect(page.locator('[data-repair-box="bad@example.com"]').getByTestId('repair-conn-state')).toHaveAttribute('data-conn', 'failed');
		await expect(page.getByTestId('repair-conn')).toContainText('Failed: 1');

		await page.getByRole('checkbox', { name: 'Select all' }).check();
		// Nothing was analysed yet, so nothing can be repaired in bulk.
		await expect(page.getByTestId('bulk-repair')).toBeDisabled();
		await page.getByTestId('bulk-scan').click();
		await expect(page.locator('[data-repair-box="clean@example.com"]').getByTestId('repair-state')).toContainText('Intact');
	});
});
