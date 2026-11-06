import { writeFileSync } from 'node:fs';
import { expect, mock, pstFile, ready, test } from './helpers';

const names = (page: import('./helpers').Page) => page.getByTestId('queue-name').allTextContents();

async function queueAction(page: import('./helpers').Page, name: string, action: string) {
	await page.locator(`li[data-upload="${name}"]`).getByRole('button', { name: `More actions for ${name}` }).click();
	await page.getByRole('menuitem', { name: action, exact: true }).click();
}

test.describe('upload queue', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('the order is chosen before anything starts, smallest first by default', async ({ page }) => {
		await mock(page.request, 'reset', { chunkDelay: 300 });
		await ready(page, '/files');
		await page.getByTestId('file-input').setInputFiles([pstFile('big.pst', 3_000_000), pstFile('small.pst', 1000), pstFile('mid.pst', 1_500_000)]);

		const preview = page.getByTestId('order-preview');
		await expect(preview.locator('li').first()).toContainText('small.pst');
		await expect(page.getByRole('radio', { name: 'Size, smallest first' })).toBeChecked();
		await page.getByRole('radio', { name: 'Size, largest first' }).check();
		await expect(preview.locator('li').first()).toContainText('big.pst');
		await page.getByRole('radio', { name: 'Name' }).check();
		await expect(preview.locator('li').first()).toContainText('big.pst');
		await expect(preview.locator('li').last()).toContainText('small.pst');
		await page.getByRole('radio', { name: 'Size, smallest first' }).check();
		await expect(page.getByRole('dialog')).toContainText('last modified date');
		await page.getByTestId('order-start').click();

		await expect.poll(() => names(page)).toEqual(['small.pst', 'mid.pst', 'big.pst']);
		await expect.poll(async () => (await mock(page.request, 'state')).uploadOrder.length, { timeout: 15000 }).toBe(3);
		expect((await mock(page.request, 'state')).uploadOrder).toEqual(['small.pst', 'mid.pst', 'big.pst']);
	});

	test('waiting files can be reordered, and one file runs at a time', async ({ page }) => {
		await mock(page.request, 'reset', { chunkDelay: 1500 });
		await ready(page, '/files');
		await page.getByTestId('file-input').setInputFiles([pstFile('a.pst', 2_500_000), pstFile('b.pst', 2_600_000), pstFile('c.pst', 2_700_000)]);
		await page.getByTestId('order-start').click();
		await expect.poll(() => names(page)).toEqual(['a.pst', 'b.pst', 'c.pst']);

		await queueAction(page, 'c.pst', 'Sooner in the queue');
		await expect.poll(() => names(page)).toEqual(['a.pst', 'c.pst', 'b.pst']);
		expect((await mock(page.request, 'state')).uploadOrder).toEqual(['a.pst']);

		await page.getByRole('button', { name: 'Pause all' }).click();
		await page.getByRole('button', { name: 'Resume all' }).click();
		await expect.poll(async () => (await mock(page.request, 'state')).uploadOrder, { timeout: 15000 }).toEqual(['a.pst', 'c.pst', 'b.pst']);
	});

	test('the queue survives navigation, cancel removes the file', async ({ page }) => {
		await mock(page.request, 'reset', { chunkDelay: 2000 });
		await ready(page, '/files');
		await page.getByTestId('file-input').setInputFiles([pstFile('stay.pst', 3_000_000), pstFile('go.pst', 3_100_000)]);
		await page.getByTestId('order-start').click();
		await expect(page.getByTestId('upload-indicator')).toBeVisible();

		await page.getByRole('link', { name: 'Migrations' }).first().click();
		await expect(page.getByTestId('upload-indicator')).toBeVisible();
		await page.getByRole('link', { name: 'Files' }).first().click();
		await expect.poll(() => names(page)).toEqual(['stay.pst', 'go.pst']);

		await queueAction(page, 'go.pst', 'Cancel');
		await expect.poll(() => names(page)).toEqual(['stay.pst']);

		// The queue is rows of the same panel as the files.
		await expect(page.getByTestId('queue').locator('xpath=..').getByTestId('pst-list')).toBeVisible();
	});

	test('after a reload the same file resumes', async ({ page }) => {
		await mock(page.request, 'reset', { chunkDelay: 400 });
		await ready(page, '/files');
		// A real file: an in-memory one gets a new modification date each time, and would be a different file.
		const file = test.info().outputPath('resume.pst');
		writeFileSync(file, Buffer.alloc(9_000_000, 7));
		await page.getByTestId('file-input').setInputFiles([file]);
		await expect(page.getByTestId('queue')).toContainText('resume.pst');

		page.on('dialog', (d) => d.accept());
		await page.reload();
		await expect(page.getByTestId('queue')).toContainText('File needed');
		await page.getByTestId('file-input').setInputFiles([file]);
		await expect(page.getByTestId('pst-list')).toContainText('resume.pst');
		await expect(page.getByTestId('queue')).toContainText('Uploaded', { timeout: 15000 });
	});

	test('the file list shows what the analysis found', async ({ page }) => {
		await ready(page, '/files');
		const list = page.getByTestId('pst-list');
		await expect(list).toContainText('alice.pst');
		await expect(page.getByTestId('pst-totals').first()).toContainText('900');
		// One panel of rows: file, kind, size, status.
		await expect(page.locator('li[data-pst="alice.pst"]').getByTestId('pst-format')).not.toBeEmpty();
		await expect(page.locator('li[data-pst="alice.pst"]')).toContainText('Valid');
		await list.getByRole('button', { name: 'More actions for alice.pst' }).click();
		await page.getByRole('menuitem', { name: 'Folders' }).click();
		await expect(page.getByRole('dialog')).toContainText('Inbox');
	});
});
