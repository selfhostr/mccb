import { writeFileSync } from 'node:fs';
import { expect, mock, pstFile, ready, row, test } from './helpers';

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

test.describe('files: migrate from a row, select, delete in bulk, filter', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	const file = (page: import('./helpers').Page, name: string) => page.locator(`li[data-pst="${name}"]`);

	test('a file is migrated to a new mailbox from its row, and the mailbox opens in its panel', async ({ page }) => {
		await ready(page, '/files');
		await file(page, 'bob.pst').getByTestId('pst-migrate').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Migrate bob.pst');
		await dialog.getByRole('textbox', { name: 'Destination address' }).fill('bob.new@example.com');
		await dialog.getByLabel('Destination password').fill('pw');
		await expect(dialog.getByTestId('migrate-submit')).toHaveText('Add the mailbox');
		await dialog.getByTestId('migrate-submit').click();

		await expect(page).toHaveURL(/\/\?account=\d+/);
		await expect(page.getByTestId('toast').filter({ hasText: 'Mailbox linked to bob.pst' })).toBeVisible();
		const panel = page.getByTestId('job-panel');
		await expect(panel).toContainText('bob.new@example.com');
		// The file is the source: the mailbox can start.
		await expect(panel.getByTestId('account-start')).toBeEnabled();
	});

	test('several mailboxes for one file come back selected on the Migrations page', async ({ page }) => {
		await ready(page, '/files');
		await file(page, 'bob.pst').getByTestId('pst-migrate').click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('migrate-row-add').click();
		const lines = dialog.getByTestId('migrate-row');
		await expect(lines).toHaveCount(2);
		for (const [i, name] of ['one', 'two'].entries()) {
			await lines.nth(i).getByRole('textbox', { name: 'Destination address' }).fill(`${name}.bob@example.com`);
			await lines.nth(i).getByLabel('Destination password').fill('pw');
		}
		await expect(dialog.getByTestId('migrate-submit')).toHaveText('Add 2 mailboxes');
		await dialog.getByTestId('migrate-submit').click();

		await expect(page).toHaveURL(/\/$/);
		await expect(page.getByTestId('toast').filter({ hasText: '2 mailboxes linked to bob.pst and selected' })).toBeVisible();
		await expect(page.getByTestId('selection-count')).toContainText('2');
		await expect(row(page, 'one.bob@example.com').getByRole('checkbox')).toBeChecked();
		await expect(row(page, 'two.bob@example.com').getByRole('checkbox')).toBeChecked();
	});

	test('a mailbox already on the server, without a source, takes the file as it is', async ({ page }) => {
		await ready(page, '/files');
		await file(page, 'bob.pst').getByTestId('pst-migrate').click();
		const dialog = page.getByRole('dialog');
		// dave is the only mailbox of Production that has no source yet.
		const existing = dialog.getByTestId('migrate-existing');
		await expect(existing).toContainText('Mailboxes already added (1)');
		await existing.locator('summary').click();
		await existing.getByRole('button', { name: 'Use', exact: true }).click();
		await expect(dialog.getByTestId('migrate-submit')).toBeEnabled();
		await dialog.getByTestId('migrate-submit').click();
		await expect(page).toHaveURL(/\/\?account=4/);
		await expect(page.getByTestId('job-panel')).toContainText('dave@example.com');
		await expect(page.getByTestId('job-panel').getByTestId('account-start')).toBeEnabled();
	});

	test('the mailboxes linked to a file are one click away', async ({ page }) => {
		await ready(page, '/files');
		// bob.pst has one mailbox: the link opens its panel.
		await expect(file(page, 'bob.pst').getByTestId('pst-linked')).toHaveText('Linked mailboxes: 1');
		await expect(file(page, 'bob.pst').getByTestId('pst-linked')).toHaveAttribute('href', '/?account=2');
		// alice.pst has two: they come back as a selection.
		await expect(file(page, 'alice.pst').getByTestId('pst-linked')).toHaveText('Linked mailboxes: 2');
		await file(page, 'alice.pst').getByTestId('pst-linked').click();
		await expect(page).toHaveURL(/\/$/);
		await expect(page.getByTestId('selection-count')).toContainText('2');
		await expect(row(page, 'alice@example.com').getByRole('checkbox')).toBeChecked();
		await expect(row(page, 'carol@example.com').getByRole('checkbox')).toBeChecked();
	});

	test('files are selected and deleted in bulk, after a confirmation', async ({ page }) => {
		await ready(page, '/files');
		await expect(page.getByTestId('pst-bar')).toHaveCount(0);
		await file(page, 'alice.pst').getByTestId('pst-select').check();
		await expect(page.getByTestId('pst-bar-count')).toHaveText('Files selected: 1');
		await page.getByTestId('pst-select-all').check();
		await expect(page.getByTestId('pst-bar-count')).toHaveText('Files selected: 2');
		await page.getByTestId('pst-select-all').uncheck();
		await expect(page.getByTestId('pst-bar')).toHaveCount(0);

		await page.getByTestId('pst-select-all').check();
		await page.getByTestId('pst-bulk-delete').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Delete these files');
		await expect(dialog).toContainText('Delete 2 files from the server?');
		await dialog.getByRole('button', { name: 'Delete 2 files' }).click();
		await expect(page.getByTestId('toast').filter({ hasText: 'Files deleted: 2' })).toBeVisible();
		await expect(page.locator('li[data-pst]')).toHaveCount(0);
		await expect(page.getByTestId('pst-bar')).toHaveCount(0);
	});

	test('the list is filtered by status, and failed files are removed at once', async ({ page }) => {
		await mock(page.request, 'pst', { pst: { id: 9, filename: 'bad.pst', size: 1000, status: 'invalid', error: 'Not a PST file', scan: null } });
		await mock(page.request, 'pst', { pst: { id: 10, filename: 'wait.pst', size: 1000, status: 'validating', scan: null } });
		await ready(page, '/files');
		const chip = (name: string) => page.getByTestId(`pst-filter-${name}`);
		await expect(chip('all')).toContainText('4');
		await expect(chip('valid')).toContainText('2');
		await expect(chip('pending')).toContainText('1');
		await expect(chip('failed')).toContainText('1');
		await expect(chip('all')).toHaveAttribute('aria-pressed', 'true');

		await chip('failed').click();
		await expect(chip('failed')).toHaveAttribute('aria-pressed', 'true');
		await expect(page.locator('li[data-pst]')).toHaveCount(1);
		await expect(file(page, 'bad.pst')).toBeVisible();
		await chip('pending').click();
		await expect(file(page, 'wait.pst')).toBeVisible();
		await expect(page.locator('li[data-pst]')).toHaveCount(1);

		await chip('failed').click();
		await page.getByTestId('pst-remove-invalid').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Remove invalid files');
		await dialog.getByRole('button', { name: 'Delete 1 files' }).click();
		await expect(page.getByTestId('toast').filter({ hasText: 'Files deleted: 1' })).toBeVisible();
		// Nothing left under this filter: the way back to the whole list is offered.
		await expect(page.getByTestId('pst-remove-invalid')).toHaveCount(0);
		await page.getByRole('button', { name: 'Show all files' }).click();
		await expect(page.locator('li[data-pst]')).toHaveCount(3);
	});
});
