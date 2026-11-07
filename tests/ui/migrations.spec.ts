import { closePanel, expect, mock, openMailbox, ready, row, rowAction, rows, test } from './helpers';

test.use({ viewport: { width: 1280, height: 800 } });

test.describe('migrations', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('select, test and import, with clicks during live events', async ({ page }) => {
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toContainText('6');

		// The panel stays on this mailbox while a job appears for it.
		const settings = await openMailbox(page, 'alice@example.com');

		// Live traffic on all rows for the whole flow.
		const burst = mock(page.request, 'burst', { rounds: 200, gap: 15 });

		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await expect(page.getByTestId('selection-count')).toContainText('2');

		await page.getByTestId('selection-bar').getByRole('button', { name: 'Test' }).click();
		await expect(row(page, 'alice@example.com').getByTestId('test-state')).toBeVisible();
		await expect(settings.getByTestId('test-result')).toContainText('CalDAV');
		await expect(row(page, 'alice@example.com').getByTestId('test-state')).toHaveCount(0);
		await expect(row(page, 'bob@example.com').getByTestId('test-state')).toHaveCount(0);
		expect((await mock(page.request, 'state')).testCalls).toEqual({ 1: 1, 2: 1 });
		await burst;

		// A job from the burst must not block the import in the mock: fresh state for this part.
		await mock(page.request, 'reset');
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await expect(page.getByTestId('toast').first()).toContainText('1');
		await expect(row(page, 'alice@example.com').locator('[data-job-link]')).toBeVisible();
		const state = await mock(page.request, 'state');
		expect(state.runCalls).toEqual([[1]]);
	});

	test('a burst of live events loses no click and rebuilds no row', async ({ page }) => {
		await ready(page);
		const alice = row(page, 'alice@example.com');
		await alice.evaluate((el) => el.setAttribute('data-marker', 'same-node'));
		const settings = await openMailbox(page, 'alice@example.com');
		const select = settings.locator('select');
		await select.evaluate((el) => el.setAttribute('data-marker', 'same-select'));
		await select.focus();

		const burst = mock(page.request, 'burst', { rounds: 150, gap: 8 });
		// Every click lands, in a panel and in rows that keep being updated.
		const toggle = settings.getByRole('button', { name: /^Import Calendar/ });
		for (let i = 0; i < 12; i++) {
			const before = await toggle.getAttribute('aria-pressed');
			await toggle.click();
			await expect(toggle).toHaveAttribute('aria-pressed', before === 'true' ? 'false' : 'true');
		}
		for (let i = 0; i < 10; i++) {
			const box = row(page, 'carol@example.com').getByRole('checkbox');
			await box.setChecked(i % 2 === 0);
			await expect(box).toBeChecked({ checked: i % 2 === 0 });
		}
		await burst;

		// The row and the select are the same DOM nodes: an open select would still be open.
		await expect(alice).toHaveAttribute('data-marker', 'same-node');
		await expect(select).toHaveAttribute('data-marker', 'same-select');
		await expect(alice.locator('[data-job-link]')).toBeVisible();
	});

	test('progress does not reorder the list, a change of state does, and what needs you comes first', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		const emails = () => page.getByTestId('account-email').allTextContents();
		await expect.poll(emails).toEqual(['alice@example.com', 'bob@example.com', 'carol@example.com', 'dave@example.com', 'erin@example.org', 'frank@example.org']);

		await row(page, 'carol@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await expect.poll(async () => (await emails())[0]).toBe('carol@example.com');

		// Progress moves the bar and nothing else: the row is the same node in the same place.
		const carol = row(page, 'carol@example.com');
		await carol.evaluate((el) => el.setAttribute('data-marker', 'same'));
		const progress = { mail: { done: 450, total: 900, errors: 0 }, calendar: { done: 0, total: 0, errors: 0 }, contacts: { done: 0, total: 0, errors: 0 } };
		await mock(page.request, 'job', { id: 1, patch: { status: 'running', progress } });
		await expect(carol.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
		await expect(carol).toHaveAttribute('data-marker', 'same');
		expect((await emails())[0]).toBe('carol@example.com');

		await mock(page.request, 'job', { id: 1, patch: { status: 'failed', error: 'connection lost' } });
		await expect(carol).toContainText('Failed');
		await mock(page.request, 'job', { id: 1, patch: { status: 'done', error: null } });
		await expect.poll(async () => (await emails()).at(-1)).toBe('carol@example.com');

		// A refused login comes before everything else, without any job.
		const account = await (await page.request.get('/api/accounts/6')).json();
		await mock(page.request, 'event', { kind: 'account', data: { ...account, status: 'auth_failed' } });
		await expect.poll(async () => (await emails())[0]).toBe('frank@example.org');
		await expect(row(page, 'frank@example.org')).toContainText('Login refused');
	});

	test('the chips filter through the URL, with counts', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();

		await expect(page.getByTestId('chip-all')).toContainText('6');
		await expect(page.getByTestId('chip-active')).toContainText('1');
		await expect(page.getByTestId('chip-all')).toHaveAttribute('aria-pressed', 'true');
		await page.getByTestId('chip-active').click();
		await expect(page).toHaveURL(/status=active/);
		await expect(page.getByTestId('chip-active')).toHaveAttribute('aria-pressed', 'true');
		await expect(rows(page)).toHaveCount(1);

		await page.reload();
		await expect(rows(page)).toHaveCount(1);
		await page.getByTestId('chip-all').click();
		await expect(rows(page)).toHaveCount(6);

		// The narrower states wait behind one button, and show by themselves when in use.
		await expect(page.getByTestId('filter-status')).toHaveCount(0);
		await page.getByTestId('filter-more').click();
		await page.getByTestId('filter-status').selectOption('no_pst');
		await expect(page).toHaveURL(/status=no_pst/);
		await page.reload();
		await expect(page.getByTestId('filter-status')).toHaveValue('no_pst');
	});

	test('the sentence says how far it is, and the bar moves with it', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toHaveText('0 of 6 mailboxes done.');
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();

		const progress = { mail: { done: 300, total: 900, errors: 0 }, calendar: { done: 0, total: 0, errors: 0 }, contacts: { done: 0, total: 0, errors: 0 } };
		await mock(page.request, 'job', { id: 1, patch: { status: 'running', progress, rate: 10 } });
		await expect(page.getByTestId('page-sentence')).toHaveText(/^0 of 6 mailboxes done\. About 1\d* min left\.$/);
		await expect(page.getByTestId('overall').getByRole('progressbar')).toHaveAttribute('aria-valuenow', '6');

		await mock(page.request, 'job', { id: 1, patch: { status: 'failed', error: 'connection lost' } });
		await expect(page.getByTestId('page-sentence')).toHaveText('0 of 6 mailboxes done, 1 needs you.');
	});

	test('a filter says what stays selected and leaves it out', async ({ page }) => {
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'erin@example.org').getByRole('checkbox').check();

		await page.getByTestId('filter-q').fill('alice');
		await expect(page).toHaveURL(/q=alice/);
		await expect(rows(page)).toHaveCount(1);
		await expect(page.getByTestId('selection-count')).toContainText('1');
		await expect(page.getByTestId('selection-hidden')).toContainText('1');
		await expect(page.getByTestId('select-all')).toBeChecked();

		await page.getByTestId('selection-bar').getByRole('button', { name: 'Test' }).click();
		await expect((await openMailbox(page, 'alice@example.com')).getByTestId('test-result')).toBeVisible();
		await closePanel(page);
		const state = await mock(page.request, 'state');
		expect(Object.keys(state.testCalls)).toEqual(['1']);

		// The filter survives a reload: it lives in the URL.
		await page.reload();
		await expect(page.getByTestId('filter-q')).toHaveValue('alice');

		await page.getByTestId('filter-q').fill('');
		await expect(rows(page)).toHaveCount(6);
	});

	test('select all follows single rows', async ({ page }) => {
		await ready(page);
		const all = page.getByTestId('select-all');
		await all.check();
		await expect(page.getByTestId('selection-count')).toContainText('6');
		await row(page, 'bob@example.com').getByRole('checkbox').uncheck();
		await expect(all).not.toBeChecked();
		expect(await all.evaluate((el: HTMLInputElement) => el.indeterminate)).toBe(true);
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await expect(all).toBeChecked();
	});

	test('a running test shows, cannot start twice, and its result survives a reload', async ({ page }) => {
		await mock(page.request, 'reset', { testDelay: 2000 });
		await ready(page);
		const bob = row(page, 'bob@example.com');
		await rowAction(page, 'bob@example.com', 'Test connection');
		await expect(bob.getByTestId('test-state')).toContainText('Testing');
		await bob.getByRole('button', { name: 'More actions for bob@example.com' }).click();
		await expect(page.getByRole('menuitem', { name: 'Testing' })).toBeDisabled();
		await page.keyboard.press('Escape');
		const settings = await openMailbox(page, 'bob@example.com');
		await expect(settings.getByTestId('account-test')).toBeDisabled();
		await expect(settings.getByTestId('test-result')).toBeVisible();
		expect((await mock(page.request, 'state')).testCalls).toEqual({ 2: 1 });

		await page.reload();
		await expect(settings.getByTestId('test-result')).toContainText('IMAP');
		await settings.getByTestId('test-result').click();
		await expect(page.getByRole('dialog')).toContainText('CalDAV');
		await expect(page.getByRole('dialog')).toContainText('HTTP 401');
	});

	test('the selection bar shifts nothing', async ({ page }) => {
		await ready(page);
		const top = async () => row(page, 'frank@example.org').evaluate((el) => el.getBoundingClientRect().top);
		const before = await top();
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await expect(page.getByTestId('selection-bar')).toBeVisible();
		expect(await top()).toBe(before);
	});

	test('job details have their own URL and every action', async ({ page }) => {
		await mock(page.request, 'reset', { jobTick: 400 });
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await row(page, 'alice@example.com').locator('[data-job-link]').click();
		await expect(page).toHaveURL(/\/jobs\/1/);
		const panel = page.getByTestId('job-panel');
		await expect(page.getByTestId('job-title')).toHaveText('alice@example.com');

		await panel.getByRole('button', { name: 'Pause' }).click();
		await expect(panel.getByRole('button', { name: 'Resume' })).toBeVisible();
		await panel.getByRole('button', { name: 'Resume' }).click();
		await expect(panel.getByRole('button', { name: 'Pause' })).toBeVisible();

		await expect(page.getByTestId('job-log')).toContainText('Job queued');
		await panel.getByRole('tab', { name: 'Errors' }).click();
		await expect(page).toHaveURL(/tab=errors/);
		await expect(page.getByTestId('job-errors')).toContainText('APPEND refused');

		// The URL opens the same view; Esc goes back to the table.
		await page.goto('/jobs/1?tab=errors');
		await expect(page.getByTestId('job-errors')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(panel).toHaveCount(0);
		await expect(page).toHaveURL(/\/$/);
	});

	test('a new migration is one form, and its panel says what is missing before the start', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		let dialog = page.getByRole('dialog');
		await expect(dialog.getByText('From', { exact: true })).toBeVisible();
		await expect(dialog.getByText('To', { exact: true })).toBeVisible();
		await dialog.getByRole('textbox', { name: /^Email address/ }).fill('new.one@example.com');
		await dialog.locator('input[type=password]').fill('pw');
		await dialog.getByRole('button', { name: 'Add a mailbox' }).click();
		await expect(dialog).toHaveCount(0);

		// Saving opens the panel of the new mailbox: no source yet, so the start waits for one.
		const panel = page.getByTestId('job-panel');
		await expect(panel).toContainText('new.one@example.com');
		await expect(panel.getByTestId('account-start')).toBeDisabled();
		await expect(panel.getByTestId('start-hint')).toHaveText('Choose where the mail comes from first.');
		await expect(row(page, 'new.one@example.com')).toContainText('No source yet');

		const select = panel.getByRole('combobox', { name: /^PST file for/ });
		await select.selectOption({ index: 1 });
		await expect(panel.getByTestId('account-start')).toBeEnabled();
		await panel.getByTestId('account-start').click();
		dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Ready to start: 1');
		await dialog.getByTestId('import-confirm').click();
		await expect(page.getByTestId('toast').filter({ hasText: 'Jobs queued: 1' })).toBeVisible();
		await expect(panel.getByTestId('account-start')).toHaveCount(0);
	});

	test('a new migration can take a file already on the server', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('new-source').selectOption({ index: 1 });
		await dialog.getByRole('textbox', { name: /^Email address/ }).fill('with.file@example.com');
		await dialog.locator('input[type=password]').fill('pw');
		await dialog.getByRole('button', { name: 'Add a mailbox' }).click();
		await expect(page.getByTestId('job-panel').getByTestId('account-start')).toBeEnabled();
	});

	test('a mailbox without a job has its own panel, with its URL', async ({ page }) => {
		await ready(page);
		await page.getByTestId('filter-q').fill('alice');
		const settings = await openMailbox(page, 'alice@example.com');
		await expect(page).toHaveURL(/q=alice/);
		await expect(page).toHaveURL(/account=1/);
		await expect(page.getByTestId('job-title')).toHaveText('alice@example.com');
		await expect(settings.getByRole('combobox', { name: 'PST file for alice@example.com' })).toBeVisible();

		await settings.getByRole('button', { name: /^Import Calendar/ }).click();
		await expect.poll(async () => (await mock(page.request, 'state')).accounts.find((a: { id: number }) => a.id === 1).options.calendar).toBe(false);

		await page.reload();
		await expect(page.getByTestId('job-panel').getByRole('combobox')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(page.getByTestId('job-panel')).toHaveCount(0);
		await expect(page).toHaveURL(/q=alice$/);
		await expect(page.locator('[data-account-link="1"]')).toBeFocused();
	});

	test('thousands of accounts: only what shows is in the DOM', async ({ page }) => {
		await mock(page.request, 'reset', { accounts: 5000 });
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toContainText('5');
		expect(await rows(page).count()).toBeLessThan(120);

		await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
		await expect(row(page, 'user05000@example.com')).toBeVisible();
		expect(await rows(page).count()).toBeLessThan(120);

		await page.getByTestId('select-all').check();
		await expect(page.getByTestId('selection-count')).toContainText(/5[\s,.  ]?000/);

		await mock(page.request, 'burst', { rounds: 3, gap: 50 });
		// Reaching the select-all box scrolled back up: the last row left the window.
		await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
		await expect(row(page, 'user05000@example.com').locator('[data-job-link]')).toBeVisible();
	});

	test('designed states: no server, no account, server error', async ({ page }) => {
		await mock(page.request, 'reset', { servers: 0 });
		await ready(page);
		await expect(page.getByTestId('empty-state')).toContainText('No server configured');

		await mock(page.request, 'reset', { accounts: 0 });
		await page.reload();
		await expect(page.getByTestId('empty-state')).toContainText('No mailboxes yet');

		await page.route('**/api/accounts', (route) => route.fulfill({ status: 500, json: { detail: 'database is locked' } }));
		await page.reload();
		await expect(page.getByRole('alert')).toContainText('database is locked');
		await page.unroute('**/api/accounts');
		await page.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('empty-state')).toContainText('No mailboxes yet');
	});

	test('old links still work', async ({ page }) => {
		await page.goto('/#/accounts?server=2');
		await expect(page).toHaveURL(/\/\?server=2$/);
		await expect(rows(page)).toHaveCount(2);
		await page.goto('/#/uploads');
		await expect(page).toHaveURL(/\/files$/);
		await page.goto('/#/servers');
		await expect(page).toHaveURL(/\/servers$/);
	});
});
