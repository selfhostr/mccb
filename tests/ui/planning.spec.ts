import { expect, mock, noOverflow, openMailbox, pstFile, ready, row, rows, test } from './helpers';
import type { Page } from './helpers';

test.use({ viewport: { width: 1280, height: 900 } });

const state = (page: Page) => mock(page.request, 'state');

async function openImport(page: Page, emails: string[]) {
	for (const email of emails) await row(page, email).getByRole('checkbox').check();
	await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
	await expect(page.getByRole('dialog')).toContainText('Ready to start');
}

async function importNamed(page: Page, emails: string[], name: string) {
	await openImport(page, emails);
	await page.getByTestId('import-options').locator('summary').click();
	await page.getByTestId('import-batch').fill(name);
	await page.getByTestId('import-confirm').click();
	await expect(page.getByRole('dialog')).toHaveCount(0);
}

test.describe('dry run and import options', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('a dry run shows each mailbox and starts nothing', async ({ page }) => {
		await ready(page);
		await openImport(page, ['alice@example.com', 'bob@example.com']);
		await page.getByTestId('import-dry').click();
		await expect(page.getByTestId('plan-running')).toBeVisible();

		const result = page.getByTestId('plan-result');
		await expect(result).toBeVisible();
		await expect(page.getByTestId('plan-totals')).toContainText('Mailboxes checked: 2');
		const accounts = page.getByTestId('plan-account');
		await expect(accounts).toHaveCount(2);

		// Status is an icon and a word, not a colour.
		await expect(accounts.nth(0)).toContainText('Ready');
		await expect(accounts.nth(1)).toContainText('To read');

		await accounts.nth(1).locator('summary').click();
		await expect(accounts.nth(1).getByTestId('plan-quota')).toContainText('Quota is too small');
		await expect(accounts.nth(1)).toContainText('quota: 3 MB free on the target');
		await expect(accounts.nth(1)).toContainText('Folders to create (1)');
		await expect(accounts.nth(1).getByTestId('plan-create')).toContainText('Archive/Old');
		await expect(accounts.nth(1).locator('tbody tr')).toHaveCount(3);

		// Nothing was written and no job exists.
		expect((await state(page)).runCalls).toEqual([]);
		// The user can still start from the same dialog.
		await page.getByTestId('import-confirm').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).runCalls).toEqual([[1, 2]]);
	});

	test('a batch name makes a batch, and the mode is asked only after a first run', async ({ page }) => {
		await mock(page.request, 'reset', { jobTick: 80 });
		await ready(page);
		await openImport(page, ['alice@example.com']);
		await page.getByTestId('import-options').locator('summary').click();
		await expect(page.getByTestId('import-mode')).toHaveCount(0);
		await page.getByTestId('import-batch').fill('Wave 1');
		await page.getByTestId('import-confirm').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await expect(row(page, 'alice@example.com')).toContainText('Done');
		expect((await state(page)).runBodies[0]).toEqual({ account_ids: [1], name: 'Wave 1' });

		await openImport(page, ['alice@example.com']);
		await page.getByTestId('import-options').locator('summary').click();
		await page.getByTestId('import-mode').selectOption('delta');
		await page.getByTestId('import-confirm').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).runBodies[1]).toEqual({ account_ids: [1], mode: 'delta' });
	});

	test('the dry run fits a phone', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 800 });
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-dry').click();
		await expect(page.getByTestId('plan-result')).toBeVisible();
		await page.getByTestId('plan-account').first().locator('summary').click();
		await noOverflow(page);
	});
});

test.describe('batches and queue', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('the batch selector appears with the first batch and filters through the URL', async ({ page }) => {
		// Jobs that resume must not finish before the stop below.
		await mock(page.request, 'reset', { hold: true, jobTick: 60000 });
		await ready(page);
		await page.getByTestId('filter-more').click();
		await expect(page.getByTestId('filter-status')).toBeVisible();
		await expect(page.getByTestId('filter-batch')).toHaveCount(0);
		await importNamed(page, ['alice@example.com', 'bob@example.com'], 'Wave 1');
		await expect(page.getByTestId('filter-batch')).toBeVisible();

		await page.getByTestId('filter-batch').selectOption({ label: 'Wave 1' });
		await expect(page).toHaveURL(/batch=1/);
		await expect(rows(page)).toHaveCount(2);
		const bar = page.getByTestId('batch-bar');
		await expect(bar).toContainText('Wave 1');
		await expect(bar).toContainText('Queued');
		await expect(page.getByTestId('batch-totals')).toContainText('Jobs: 2');

		await page.reload();
		await expect(page.getByTestId('filter-batch')).toHaveValue('1');
		await expect(rows(page)).toHaveCount(2);

		// Only what would change something is offered.
		await expect(page.getByTestId('batch-resume')).toHaveCount(0);
		await page.getByTestId('batch-pause').click();
		await expect(page.getByTestId('batch-resume')).toBeVisible();
		await expect(bar).toContainText('Paused');
		await expect(row(page, 'alice@example.com')).toContainText('Paused');
		await page.getByTestId('batch-resume').click();
		await expect(page.getByTestId('batch-pause')).toBeVisible();

		await page.getByTestId('batch-stop').click();
		await page.getByRole('dialog').getByRole('button', { name: 'Stop' }).click();
		await expect(row(page, 'alice@example.com')).toContainText('Stopping');

		await page.getByTestId('filter-batch').selectOption('');
		await expect(rows(page)).toHaveCount(6);
	});

	test('retry failed reruns the failed jobs of the batch', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await importNamed(page, ['alice@example.com'], 'Wave 2');
		await mock(page.request, 'job', { id: 1, patch: { status: 'failed', error: 'connection lost' } });
		await page.getByTestId('filter-more').click();
		await page.getByTestId('filter-batch').selectOption({ label: 'Wave 2' });
		await expect(page.getByTestId('batch-bar')).toContainText('Failed');
		await page.getByTestId('batch-retry-failed').click();
		await expect(page.getByTestId('toast').last()).toContainText('Jobs changed: 1');
	});

	test('a queued job moves up and down from its row menu', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await openImport(page, ['alice@example.com', 'bob@example.com', 'carol@example.com']);
		await page.getByTestId('import-confirm').click();
		await expect(row(page, 'carol@example.com')).toContainText('Queued');

		await row(page, 'carol@example.com').getByRole('button', { name: 'More actions for carol@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Sooner in the queue' }).click();
		await expect(page.getByTestId('toast').last()).toContainText('Moved up');
		expect((await state(page)).reorderCalls).toEqual([[1, 3, 2]]);

		// carol is second now; alice first cannot go further up.
		await row(page, 'alice@example.com').getByRole('button', { name: 'More actions for alice@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Sooner in the queue' }).click();
		await row(page, 'bob@example.com').getByRole('button', { name: 'More actions for bob@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Later in the queue' }).click();
		expect((await state(page)).reorderCalls).toHaveLength(1);

		await row(page, 'carol@example.com').getByRole('button', { name: 'More actions for carol@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Later in the queue' }).click();
		await expect.poll(async () => (await state(page)).reorderCalls).toEqual([[1, 3, 2], [1, 2, 3]]);

		// A row without a queued job has no queue entries.
		await row(page, 'erin@example.org').getByRole('button', { name: 'More actions for erin@example.org' }).click();
		await expect(page.getByRole('menuitem', { name: 'Sooner in the queue' })).toHaveCount(0);
	});

	test('a job waiting for the allowed hours is not an error', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await openImport(page, ['alice@example.com']);
		await page.getByTestId('import-confirm').click();
		await mock(page.request, 'job', { id: 1, patch: { status: 'paused', pause_reason: 'window' } });
		await expect(row(page, 'alice@example.com')).toContainText('Waiting for allowed hours');
		await expect(row(page, 'alice@example.com')).not.toContainText('Failed');
		await row(page, 'alice@example.com').locator('[data-job-link]').click();
		await expect(page.getByTestId('job-panel')).toContainText('Waiting for allowed hours');

		// Paused by the administrator reads as a plain pause.
		await mock(page.request, 'job', { id: 1, patch: { pause_reason: null } });
		await expect(page.getByTestId('job-panel')).not.toContainText('Waiting for allowed hours');
		await expect(page.getByTestId('job-panel')).toContainText('Paused');
	});

	test('the batch bar fits a phone', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 800 });
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await importNamed(page, ['alice@example.com'], 'A fairly long batch name for a small screen');
		await page.getByTestId('filter-more').click();
		await page.getByTestId('filter-batch').selectOption({ index: 1 });
		await expect(page.getByTestId('batch-bar')).toBeVisible();
		await noOverflow(page);
	});
});

test.describe('job panel', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	async function finishedJob(page: Page) {
		await mock(page.request, 'reset', { jobTick: 60 });
		await ready(page);
		await openImport(page, ['alice@example.com']);
		await page.getByTestId('import-confirm').click();
		await expect(row(page, 'alice@example.com')).toContainText('Done');
	}

	test('compare, with its table and CSV, and the items left out', async ({ page }) => {
		await finishedJob(page);
		await page.goto('/jobs/1?tab=report');
		const compare = page.getByTestId('job-compare');
		await expect(compare).toBeVisible();
		await compare.getByTestId('compare-start').click();
		await expect(compare.getByTestId('compare-start')).toBeDisabled();
		const table = compare.getByTestId('compare-table');
		await expect(table).toBeVisible();
		await expect(table).toContainText('INBOX');
		await expect(compare).toContainText('Missing on the target: 2');
		await expect(compare.getByRole('link', { name: 'Download as CSV' })).toHaveAttribute('href', '/api/jobs/1/compare/csv');
		expect((await state(page)).compareCalls).toEqual([1]);

		// The latest comparison is still there after a reload.
		await page.reload();
		await expect(page.getByTestId('compare-table')).toBeVisible();

		await page.getByTestId('leftout-filtered').locator('summary').click();
		await expect(page.getByTestId('leftout-filtered')).toContainText('Old newsletter');
		await expect(page.getByTestId('leftout-filtered')).toContainText('date');
		await page.getByTestId('leftout-oversized').locator('summary').click();
		await expect(page.getByTestId('leftout-oversized')).toContainText('Holiday video');
		await expect(page.getByTestId('leftout-oversized')).toContainText('50 MB');
	});

	test('a final resync is one click, and tasks show when the route has them', async ({ page }) => {
		await finishedJob(page);
		await page.goto('/jobs/1');
		await page.getByTestId('job-delta').click();
		await expect(page.getByTestId('toast').last()).toContainText('Final resync queued');
		expect((await state(page)).deltaCalls).toEqual([1]);

		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await openImport(page, ['alice@example.com']);
		await page.getByTestId('import-confirm').click();
		await mock(page.request, 'job', { id: 1, patch: { progress: { mail: { done: 1, total: 4, errors: 0 }, calendar: { done: 0, total: 0, errors: 0 }, contacts: { done: 0, total: 0, errors: 0 }, tasks: { done: 2, total: 5, errors: 0 } } } });
		await page.goto('/jobs/1');
		const panel = page.getByTestId('job-panel');
		await expect(panel).toContainText('Tasks');
		await expect(panel).toContainText('2 of 5');
		await expect(page.getByTestId('job-delta')).toHaveCount(0);
	});
});

test.describe('policies', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('a server: rules, hours and bandwidth, with the reason when the server refuses', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'More actions for Production' }).click();
		await page.getByRole('menuitem', { name: 'Edit' }).click();
		const dialog = page.getByRole('dialog');
		// Nothing about policies is loaded or shown until the disclosure opens.
		await expect(dialog.getByTestId('policy-max-size')).toBeHidden();
		await dialog.getByTestId('policy').locator('summary').click();

		await dialog.getByTestId('policy-max-size').fill('-5');
		await dialog.getByTestId('server-save').click();
		await expect(dialog).toContainText('max_size_mb must be positive');
		expect((await state(page)).policies.server).toEqual({});

		await dialog.getByTestId('policy-max-size').fill('25');
		await dialog.getByTestId('policy-exclude').fill('Junk*\nDeleted Items');
		await dialog.getByTestId('policy-map-add').click();
		await dialog.getByTestId('policy-map-source').fill('Sent Items');
		await dialog.getByTestId('policy-map-target').fill('@sent');
		await dialog.getByTestId('policy-start').fill('20:00');
		await dialog.getByTestId('policy-end').fill('06:00');
		await dialog.getByTestId('policy-tz').fill('Europe/Paris');
		await dialog.getByRole('button', { name: 'Mon', exact: true }).click();
		await dialog.getByTestId('policy-rate').fill('5');
		await dialog.getByTestId('server-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);

		const saved = (await state(page)).policies.server['1'];
		expect(saved).toEqual({
			folder_map: [{ source: 'Sent Items', target: '@sent' }],
			exclude_folders: ['Junk*', 'Deleted Items'],
			max_size_mb: 25,
			window: { start: '20:00', end: '06:00', tz: 'Europe/Paris', days: [0] },
			rate_mb_s: 5
		});

		// Opened again, the dialog shows what is stored.
		await page.getByRole('button', { name: 'More actions for Production' }).click();
		await page.getByRole('menuitem', { name: 'Edit' }).click();
		await page.getByRole('dialog').getByTestId('policy').locator('summary').click();
		await expect(page.getByRole('dialog').getByTestId('policy-max-size')).toHaveValue('25');
		await expect(page.getByRole('dialog').getByTestId('policy-start')).toHaveValue('20:00');
	});

	test('an account: its effective values, and no hours or bandwidth', async ({ page }) => {
		await page.request.put('/api/servers/1/policy', { data: { max_size_mb: 25, exclude_folders: ['Junk*'], rate_mb_s: 4 } });
		await ready(page);
		await row(page, 'alice@example.com').getByRole('button', { name: 'More actions for alice@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Edit password or login' }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('policy').locator('summary').click();
		const effective = dialog.getByTestId('policy-effective');
		await expect(effective).toContainText('25');
		await expect(effective).toContainText('Junk*');
		await expect(dialog.getByTestId('policy-start')).toHaveCount(0);
		await expect(dialog.getByTestId('policy-rate')).toHaveCount(0);

		await dialog.getByTestId('policy-date-from').fill('2024-01-01');
		await dialog.getByTestId('policy-date-to').fill('2023-01-01');
		await dialog.getByRole('button', { name: 'Save' }).click();
		await expect(dialog).toContainText('date_from is after date_to');

		await dialog.getByTestId('policy-date-to').fill('2024-12-31');
		await dialog.getByRole('button', { name: 'Save' }).click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).policies.account['1']).toEqual({ date_from: '2024-01-01', date_to: '2024-12-31' });
	});

	test('a dialog that was not touched writes no policy', async ({ page }) => {
		await ready(page);
		await row(page, 'alice@example.com').getByRole('button', { name: 'More actions for alice@example.com' }).click();
		await page.getByRole('menuitem', { name: 'Edit password or login' }).click();
		await page.getByRole('dialog').getByTestId('policy').locator('summary').click();
		await expect(page.getByRole('dialog').getByTestId('policy-date-from')).toBeVisible();
		await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await state(page)).policies.account).toEqual({});
	});

	test('the server dialog fits a phone', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 800 });
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'More actions for Production' }).click();
		await page.getByRole('menuitem', { name: 'Edit' }).click();
		await page.getByRole('dialog').getByTestId('policy').locator('summary').click();
		await page.getByRole('dialog').getByTestId('policy-map-add').click();
		await noOverflow(page);
		const box = await page.getByRole('dialog').getByTestId('policy-map-add').boundingBox();
		expect(box!.height).toBeGreaterThanOrEqual(40);
	});
});

test.describe('API tokens', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('create, copy once, revoke', async ({ page, context }) => {
		await context.grantPermissions(['clipboard-read', 'clipboard-write']);
		await ready(page, '/settings');
		const section = page.getByTestId('section-tokens');
		await expect(section).toContainText('No token yet');
		await section.getByTestId('token-name').fill('nas');
		await section.getByTestId('token-create').click();

		const created = section.getByTestId('token-created');
		await expect(created.getByTestId('token-value')).toContainText('mccb_');
		await expect(created).toContainText('shown only once');
		await created.getByRole('button', { name: 'Copy the token' }).click();
		await expect(created.getByRole('button', { name: 'Copied', exact: true })).toBeVisible();
		await created.getByTestId('token-hide').click();
		await expect(created).toHaveCount(0);

		// The list never shows the token again.
		const item = section.locator('[data-token="nas"]');
		await expect(item).toContainText('Active');
		await expect(item).not.toContainText('xxxxxxxx');
		await page.reload();
		await expect(page.getByTestId('token-value')).toHaveCount(0);

		await page.getByRole('button', { name: 'Revoke nas' }).click();
		await page.getByRole('dialog').getByRole('button', { name: 'Revoke', exact: true }).click();
		await expect(item).toContainText('Revoked');
		await expect(page.getByRole('button', { name: 'Revoke nas' })).toHaveCount(0);
	});

	test('the section fits a phone', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 800 });
		await ready(page, '/settings');
		await page.getByTestId('token-name').fill('a token with a rather long descriptive name');
		await page.getByTestId('token-create').click();
		await expect(page.getByTestId('token-created')).toBeVisible();
		await noOverflow(page);
	});
});

test.describe('accounts as CSV and files of every kind', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('export sits next to import, and import shows its warnings', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		await page.getByRole('dialog').getByTestId('new-csv').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('csv-export')).toHaveAttribute('href', '/api/accounts/export-csv');
		await dialog.locator('input[type=file]').setInputFiles({ name: 'mailboxes.csv', mimeType: 'text/csv', buffer: Buffer.from('email;password;pst\nx@example.com;pw;missing.pst\n') });
		await dialog.getByRole('button', { name: 'Import', exact: true }).click();
		await expect(dialog.getByTestId('csv-result')).toBeVisible();
		await expect(dialog.getByTestId('csv-warnings')).toContainText('missing.pst');
		await expect(dialog.getByTestId('csv-warnings')).toContainText('Line 4');
	});

	test('every kind of file is accepted, and a single one goes straight to the queue', async ({ page }) => {
		await mock(page.request, 'reset', { chunkDelay: 3000 });
		await ready(page, '/files');
		const accept = await page.getByTestId('file-input').getAttribute('accept');
		for (const ext of ['.pst', '.ost', '.mbox', '.mbx', '.eml', '.msg', '.ics', '.vcf', '.zip']) expect(accept).toContain(ext);
		await expect(page.getByTestId('dropzone')).not.toContainText('PST or OST files');

		await page.getByTestId('file-input').setInputFiles([pstFile('mail.eml', 2000)]);
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await expect.poll(() => page.getByTestId('queue-name').allTextContents()).toEqual(['mail.eml']);

		// Several files ask for the order, and no warning about their endings.
		await page.getByTestId('file-input').setInputFiles([pstFile('a.mbox', 2000), pstFile('b.zip', 3000)]);
		await expect(page.getByTestId('order-start')).toBeVisible();
		await expect(page.getByRole('dialog')).not.toContainText('may fail the analysis');
	});

	test('a file that is not a PST says its format and has no preview', async ({ page }) => {
		await mock(page.request, 'pst', { pst: { id: 9, filename: 'archive.zip', size: 5000, scan: { format: 'files', contents: ['mbox', 'eml'], folders: [{ path: ['Inbox'], kind: 'mail', count: 3 }], totals: { mail: 3, calendar: 0, contacts: 0 }, errors: [] } } });
		await ready(page, '/files');
		const zip = page.locator('[data-pst="archive.zip"]');
		await expect(zip.getByTestId('pst-format')).toHaveText('mbox, eml');
		await zip.getByRole('button', { name: 'More actions for archive.zip' }).click();
		await expect(page.getByRole('menuitem', { name: 'Delete' })).toBeVisible();
		await expect(page.getByRole('menuitem', { name: /Download a sample/ })).toHaveCount(0);
		await page.keyboard.press('Escape');

		const alice = page.locator('[data-pst="alice.pst"]');
		await alice.getByRole('button', { name: 'More actions for alice.pst' }).click();
		await expect(page.getByRole('menuitem', { name: /Download a sample/ })).toBeVisible();
	});

	test('an account that reads an uploaded file is shown like a PST account', async ({ page }) => {
		await mock(page.request, 'pst', { pst: { id: 9, filename: 'archive.zip', size: 5000, scan: { format: 'files', contents: ['mbox'], folders: [{ path: ['Inbox'], kind: 'mail', count: 3 }], totals: { mail: 3, calendar: 0, contacts: 0 }, errors: [] } } });
		await ready(page);
		const account = await (await page.request.get('/api/accounts/1')).json();
		await mock(page.request, 'event', { kind: 'account', data: { ...account, pst_id: 9, source: { kind: 'files', config: { pst_id: 9 }, has_secret: false } } });
		const alice = row(page, 'alice@example.com');
		await expect(alice).toContainText('archive.zip to');
		const settings = await openMailbox(page, 'alice@example.com');
		await expect(settings.getByRole('combobox', { name: 'PST file for alice@example.com' })).toHaveValue('9');
		await expect(settings.getByTestId('source-summary')).toHaveCount(0);

		// Selected for an import, it needs no other source.
		await alice.getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await expect(page.getByRole('dialog')).toContainText('Ready to start: 1');
	});
});
