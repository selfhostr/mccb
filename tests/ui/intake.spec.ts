import { expect, mock, ready, test } from './helpers';

test.use({ viewport: { width: 1280, height: 900 } });

const state = (page: import('./helpers').Page) => mock(page.request, 'state');

test.describe('settings: notifications', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('ntfy first, with a plain warning about public topics and write-only secrets', async ({ page }) => {
		await ready(page, '/settings');
		const section = page.getByTestId('section-notifications');
		await expect(section.getByTestId('notifications-form')).toBeVisible();
		// Nothing typed yet: no warning, and nothing to save.
		await expect(page.getByTestId('ntfy-public-warning')).toHaveCount(0);
		await expect(page.getByTestId('notify-save')).toBeDisabled();

		await expect(page.getByTestId('ntfy-topic')).toHaveCount(0);
		await section.getByRole('checkbox', { name: 'Use ntfy' }).check();
		await page.getByTestId('ntfy-topic').fill('mccb-7f3k9');
		await expect(page.getByTestId('ntfy-public-warning')).toContainText('anyone who knows the topic name can read');
		await page.getByTestId('ntfy-url').fill('https://ntfy.example.org');
		await expect(page.getByTestId('ntfy-public-warning')).toHaveCount(0);
		await page.getByTestId('ntfy-url').fill('https://ntfy.sh');

		await section.getByRole('checkbox', { name: /^Send notifications/ }).check();
		await page.getByTestId('ntfy-token').fill('tk_secret');
		await page.getByTestId('notify-save').click();
		await expect(page.getByTestId('toast').first()).toContainText('Notification settings saved');

		let saved = await state(page);
		expect(saved.intakePuts.at(-1).notifications.ntfy).toMatchObject({ enabled: true, topic: 'mccb-7f3k9', token: 'tk_secret' });
		expect(saved.intake.notifications.ntfy.has_token).toBe(true);

		// The token is never shown again: the field says it is saved, and an untouched save omits it.
		await expect(page.getByTestId('ntfy-token')).toHaveAttribute('placeholder', 'Saved. Type to replace it.');
		await expect(page.getByTestId('ntfy-token')).toHaveValue('');
		await page.getByTestId('ntfy-topic').fill('mccb-other');
		await page.getByTestId('notify-save').click();
		await expect(page.getByTestId('notify-save')).toBeDisabled();
		saved = await state(page);
		expect('token' in saved.intakePuts.at(-1).notifications.ntfy).toBe(false);
		expect(saved.intake.notifications.ntfy.has_token).toBe(true);

		// Clearing is explicit.
		await section.getByRole('button', { name: 'Clear' }).click();
		await expect(section.getByText('Will be removed when you save.')).toBeVisible();
		await page.getByTestId('notify-save').click();
		await expect.poll(async () => (await state(page)).intake.notifications.ntfy.has_token).toBe(false);
		saved = await state(page);
		expect(saved.intakePuts.at(-1).notifications.ntfy.token).toBe('');
	});

	test('webhook and e-mail are tucked away, the test shows each channel', async ({ page }) => {
		await ready(page, '/settings');
		const form = page.getByTestId('notifications-form');
		await expect(form.getByTestId('webhook-url')).toBeHidden();
		await form.getByText('Webhook and e-mail').click();
		await expect(form.getByTestId('webhook-url')).toBeVisible();

		await form.getByRole('checkbox', { name: 'Use ntfy' }).check();
		await page.getByTestId('ntfy-topic').fill('mccb-7f3k9');
		await form.getByRole('checkbox', { name: 'Send to a webhook' }).check();
		await page.getByTestId('webhook-url').fill('https://hooks.example.com/fail');
		// The test uses what is stored: edits are saved first.
		await page.getByTestId('notify-test').click();
		const results = page.getByTestId('notify-results');
		await expect(results.locator('[data-channel="ntfy"]')).toContainText('Delivered');
		await expect(results.locator('[data-channel="webhook"]')).toContainText('ConnectError');
		expect((await state(page)).notifyTests).toBe(1);

		// The summary of the disclosure says what is on.
		await expect(form.locator('summary', { hasText: 'Webhook and e-mail' })).toContainText('Webhook');
	});

	test('with nothing enabled the test is not offered, and the server refusal is shown', async ({ page }) => {
		await ready(page, '/settings');
		await expect(page.getByTestId('notify-test')).toBeDisabled();
		await page.getByTestId('ntfy-topic').fill('abc');
		await page.getByTestId('notifications-form').getByRole('checkbox', { name: /^Send notifications/ }).check();
		await page.getByTestId('notifications-form').getByRole('checkbox', { name: 'Use ntfy' }).check();
		await page.getByTestId('ntfy-topic').fill('');
		await page.getByTestId('notify-save').click();
		await expect(page.getByRole('alert')).toContainText('ntfy.topic is required');
	});

	test('events are grouped and toggles are saved', async ({ page }) => {
		await ready(page, '/settings');
		const form = page.getByTestId('notifications-form');
		const events = page.getByTestId('notify-events');
		await expect(events.locator('summary')).toContainText('On: 7');
		await events.locator('summary').click();
		for (const group of ['Files', 'Migrations', 'Progress of a migration', 'Server']) await expect(events.locator('legend', { hasText: group })).toBeVisible();
		await events.getByRole('checkbox', { name: 'A migration was stopped' }).check();
		await expect(events.locator('summary')).toContainText('On: 8');
		await form.getByRole('checkbox', { name: /^Hide addresses in messages/ }).check();
		await page.getByTestId('notify-save').click();
		await expect(page.getByTestId('toast').first()).toBeVisible();
		const saved = await state(page);
		expect(saved.intake.notifications.events.job_stopped).toBe(true);
		expect(saved.intake.notifications.mask_addresses).toBe(true);
	});
});

test.describe('settings: drop folder and automatic import', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('the drop folder path can be copied, the toggles save at once', async ({ page }) => {
		await ready(page, '/settings');
		const section = page.getByTestId('section-intake');
		await expect(section.getByTestId('intake-path')).toHaveText('/data/inbox');
		await section.getByRole('button', { name: 'Copy the folder path' }).click();
		await expect(section.getByRole('button', { name: 'Copied' })).toBeVisible();

		const inbox = section.getByRole('checkbox', { name: /^Watch the drop folder/ });
		await expect(inbox).toBeChecked();
		await inbox.uncheck();
		await expect.poll(async () => (await state(page)).intake.inbox.enabled).toBe(false);
	});

	test('automatic assignment, the import option and the default domains', async ({ page }) => {
		await ready(page, '/settings');
		const section = page.getByTestId('section-intake');
		await expect(section.getByTestId('intake-chain-options')).toHaveCount(0);
		await section.getByRole('checkbox', { name: /^Assign files to accounts automatically/ }).check();
		await expect(section.getByTestId('intake-chain-options')).toBeVisible();
		await section.getByRole('checkbox', { name: /^Start the import too/ }).check();
		await expect.poll(async () => (await state(page)).intake.auto_chain).toMatchObject({ enabled: true, start_import: true });

		await section.getByText('Default domain per server').click();
		await page.getByTestId('domain-1').fill('@Example.com');
		await page.getByTestId('domain-1').press('Tab');
		await expect.poll(async () => (await state(page)).intake.auto_chain.default_domains).toEqual({ '1': 'Example.com' });
		await expect(section.locator('summary', { hasText: 'Default domain per server' })).toContainText('Set: 1');

		// A refusal puts the field back and says why.
		await page.getByTestId('domain-2').fill('not a domain!');
		await page.getByTestId('domain-2').press('Tab');
		await expect(page.getByTestId('toast').first()).toContainText('invalid domain');
		await expect(page.getByTestId('domain-2')).toHaveValue('');
	});
});

test.describe('settings: secondary options', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('a disclosure shows a chevron and the options when opened', async ({ page }) => {
		await ready(page, '/settings');
		const disclosure = page.getByTestId('appearance-more');
		await expect(disclosure.locator('summary svg')).toBeVisible();
		await expect(disclosure.getByRole('checkbox', { name: /Mask addresses/ })).toBeHidden();
		await disclosure.locator('summary').click();
		await expect(disclosure.getByRole('checkbox', { name: /Mask addresses/ })).toBeVisible();
		await expect(disclosure.getByTestId('density')).toBeVisible();
	});
});

test.describe('files: fetch, drop folder and statuses', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	const fetching = { id: 50, filename: 'jane.pst', size: 0, status: 'fetching', source: 'fetch', origin: 'files.example.com', received_bytes: 1_048_576, chain: null, scan: null, error: null };

	test('a download shows its progress, and cancelling removes it', async ({ page }) => {
		await mock(page.request, 'pst', { pst: fetching });
		await ready(page, '/files');
		const row = page.locator('li[data-pst="jane.pst"]');
		await expect(row).toContainText('Downloading');
		await expect(row.getByTestId('pst-from')).toHaveText('Fetched from files.example.com');
		await expect(row.getByTestId('pst-fetch')).toContainText('Received');

		// Live: the total becomes known and the bar moves without the row being rebuilt.
		await row.evaluate((el) => el.setAttribute('data-marker', 'same'));
		await mock(page.request, 'pst', { pst: { id: 50, size: 4_194_304, received_bytes: 2_097_152 } });
		await expect(row.getByTestId('pst-fetch')).toContainText('50%');
		await expect(row.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
		await expect(row).toHaveAttribute('data-marker', 'same');

		await row.getByRole('button', { name: 'Cancel the download of jane.pst' }).click();
		await expect(row).toHaveCount(0);
		expect((await state(page)).cancelCalls).toEqual([50]);
	});

	test('a cancel that takes time shows Cancelling, no delete while fetching', async ({ page }) => {
		await mock(page.request, 'pst', { pst: { ...fetching, slowCancel: true } });
		await ready(page, '/files');
		const row = page.locator('li[data-pst="jane.pst"]');
		// Nothing to delete while it downloads: the menu is not offered.
		await expect(row.getByRole('button', { name: 'More actions for jane.pst' })).toHaveCount(0);
		await row.getByRole('button', { name: 'Cancel the download of jane.pst' }).click();
		await expect(row).toContainText('Cancelling');
		await expect(row.getByRole('button', { name: /^Cancel the download/ })).toHaveCount(0);
	});

	test('a failed download says why and can be retried', async ({ page }) => {
		await mock(page.request, 'pst', { pst: { ...fetching, status: 'fetch_failed', error: 'HTTP 403 from files.example.com' } });
		await ready(page, '/files');
		const row = page.locator('li[data-pst="jane.pst"]');
		await expect(row).toContainText('Download failed');
		await expect(row).toContainText('HTTP 403 from files.example.com');
		await row.getByRole('button', { name: 'Retry the download of jane.pst' }).click();
		await expect(row).toContainText('Downloading');
		expect((await state(page)).retryCalls).toEqual([50]);
	});

	test('where a file came from, and what the automatic assignment did, in words', async ({ page }) => {
		const base = { size: 2_000_000, status: 'valid', source: 'inbox', origin: 'inbox', received_bytes: null };
		await mock(page.request, 'pst', { pst: { ...base, id: 60, filename: 'a.pst', chain: { result: 'assigned', account_id: 1, job_id: 4 } } });
		await mock(page.request, 'pst', { pst: { ...base, id: 61, filename: 'b.pst', chain: { result: 'no_match' } } });
		await mock(page.request, 'pst', { pst: { ...base, id: 62, filename: 'c.pst', chain: { result: 'ambiguous' } } });
		await mock(page.request, 'pst', { pst: { ...base, id: 63, filename: 'd.pst', chain: { result: 'error', error: 'queue full' } } });
		await mock(page.request, 'pst', { pst: { ...base, id: 64, filename: 'e.pst', chain: { result: 'brand_new_result' } } });
		await mock(page.request, 'pst', { pst: { ...base, id: 65, filename: 'f.pst', source: 'upload', chain: null } });
		await ready(page, '/files');
		const li = (name: string) => page.locator(`li[data-pst="${name}"]`);
		await expect(li('a.pst').getByTestId('pst-from')).toHaveText('Taken from the drop folder');
		await expect(li('a.pst').getByTestId('pst-chain')).toHaveText('Automatically assigned to alice@example.com, import started.');
		await expect(li('b.pst').getByTestId('pst-chain')).toHaveText('No account matches this file name.');
		await expect(li('c.pst').getByTestId('pst-chain')).toContainText('Several accounts match');
		await expect(li('d.pst').getByTestId('pst-chain')).toHaveText('Automatic assignment failed: queue full');
		await expect(li('e.pst').getByTestId('pst-chain')).toHaveText('Automatic assignment: brand_new_result');
		await expect(li('f.pst').getByTestId('pst-from')).toHaveCount(0);
		await expect(li('f.pst').getByTestId('pst-chain')).toHaveCount(0);
	});

	test('"Fetch from a server": HTTP with options tucked away, SFTP and S3 fields, refusals shown', async ({ page }) => {
		await ready(page, '/files');
		await page.getByTestId('fetch-open').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.locator('[data-field="token"]')).toBeHidden();

		await page.getByTestId('fetch-submit').click();
		await expect(dialog.getByRole('alert')).toContainText('Fill in: URL.');

		await dialog.locator('[data-field="url"]').fill('http://169.254.169.254/x.pst');
		await page.getByTestId('fetch-submit').click();
		await expect(dialog.getByRole('alert')).toContainText('destination refused');

		await dialog.locator('[data-field="url"]').fill('https://files.example.com/jane.pst');
		await dialog.getByText('More options').click();
		await dialog.locator('[data-field="token"]').fill('secret-token');
		await page.getByTestId('fetch-submit').click();
		await expect(page.getByTestId('toast').first()).toContainText('Download started');
		await expect(dialog).toHaveCount(0);
		const call = (await state(page)).fetchCalls.at(-1);
		expect(call).toEqual({ kind: 'http', url: 'https://files.example.com/jane.pst', token: 'secret-token' });
		await expect(page.locator('li[data-pst="jane.pst"]')).toContainText('Downloading');

		await page.getByTestId('fetch-open').click();
		await page.getByTestId('fetch-kind').selectOption('sftp');
		await expect(page.getByRole('dialog').locator('[data-field="host"]')).toBeVisible();
		await page.getByRole('dialog').locator('[data-field="host"]').fill('files.example.com');
		await page.getByRole('dialog').locator('[data-field="path"]').fill('/backups/bob.pst');
		await page.getByRole('dialog').locator('[data-field="username"]').fill('backup');
		await page.getByTestId('fetch-submit').click();
		await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Password');
		await page.getByRole('dialog').locator('[data-field="password"]').fill('pw');
		await page.getByRole('dialog').getByText('More options').click();
		await page.getByRole('dialog').locator('[data-field="port"]').fill('2222');
		await page.getByTestId('fetch-submit').click();
		await expect(page.locator('li[data-pst="bob.pst"]')).toBeVisible();
		expect((await state(page)).fetchCalls.at(-1)).toEqual({ kind: 'sftp', host: 'files.example.com', path: '/backups/bob.pst', username: 'backup', password: 'pw', port: 2222 });

		await page.getByTestId('fetch-open').click();
		await page.getByTestId('fetch-kind').selectOption('s3');
		for (const name of ['endpoint', 'bucket', 'key', 'access_key', 'secret_key']) await expect(page.getByRole('dialog').locator(`[data-field="${name}"]`)).toBeVisible();
		await page.getByTestId('fetch-submit').click();
		await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Endpoint');
	});

	test('the drop folder lists what is waiting and refreshes', async ({ page }) => {
		await mock(page.request, 'inbox', { files: [{ name: 'jane.pst', size: 2_000_000, modified_ago: 12 }] });
		await ready(page, '/files');
		const inbox = page.getByTestId('inbox');
		await expect(inbox.locator('summary')).toContainText('Waiting: 1');
		await inbox.locator('summary').click();
		await expect(inbox.getByTestId('inbox-path')).toHaveText('/data/inbox');
		await expect(inbox.getByTestId('inbox-files')).toContainText('jane.pst');
		await expect(inbox.getByTestId('inbox-files')).toContainText('changed');
		await mock(page.request, 'inbox', { files: [] });
		await expect(inbox.locator('summary')).toContainText('Waiting: 0', { timeout: 10_000 });
		await expect(inbox).toContainText('Nothing is waiting');
	});

	test('the drop folder is hidden when it is turned off', async ({ page }) => {
		await ready(page, '/settings');
		await page.getByTestId('section-intake').getByRole('checkbox', { name: /^Watch the drop folder/ }).uncheck();
		await expect.poll(async () => (await state(page)).intake.inbox.enabled).toBe(false);
		await page.getByRole('link', { name: 'Files' }).first().click();
		await expect(page.getByTestId('dropzone')).toBeVisible();
		await expect(page.getByTestId('inbox')).toHaveCount(0);
	});
});
