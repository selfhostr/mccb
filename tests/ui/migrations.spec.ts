import { expect, mock, ready, row, test } from './helpers';

test.use({ viewport: { width: 1280, height: 800 } });

test.describe('migrations', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('select, test and import, with clicks during live events', async ({ page }) => {
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toContainText('6');

		// Live traffic on all rows for the whole flow.
		const burst = mock(page.request, 'burst', { rounds: 200, gap: 15 });

		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await expect(page.getByTestId('selection-count')).toContainText('2');

		await page.getByTestId('selection-bar').getByRole('button', { name: 'Test' }).click();
		await expect(row(page, 'alice@example.com').getByTestId('test-state')).toBeVisible();
		await expect(row(page, 'alice@example.com').getByTestId('test-result')).toBeVisible();
		await expect(row(page, 'bob@example.com').getByTestId('test-result')).toContainText('CalDAV');
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
		await alice.locator('select').evaluate((el) => el.setAttribute('data-marker', 'same-select'));
		await alice.locator('select').focus();

		const burst = mock(page.request, 'burst', { rounds: 150, gap: 8 });
		// Every click lands, in a row that keeps being updated.
		const toggle = alice.getByRole('button', { name: /^Import Calendar/ });
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

		// The row and its select are the same DOM nodes: an open select would still be open.
		await expect(alice).toHaveAttribute('data-marker', 'same-node');
		await expect(alice.locator('select')).toHaveAttribute('data-marker', 'same-select');
		await expect(alice.locator('[data-job-link]')).toBeVisible();
	});

	test('a filter says what stays selected and leaves it out', async ({ page }) => {
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'erin@example.org').getByRole('checkbox').check();

		await page.getByTestId('filter-q').fill('alice');
		await expect(page).toHaveURL(/q=alice/);
		await expect(page.locator('tr.acct-row')).toHaveCount(1);
		await expect(page.getByTestId('selection-count')).toContainText('1');
		await expect(page.getByTestId('selection-hidden')).toContainText('1');
		await expect(page.getByTestId('select-all')).toBeChecked();

		await page.getByTestId('selection-bar').getByRole('button', { name: 'Test' }).click();
		await expect(row(page, 'alice@example.com').getByTestId('test-result')).toBeVisible();
		const state = await mock(page.request, 'state');
		expect(Object.keys(state.testCalls)).toEqual(['1']);

		// The filter survives a reload: it lives in the URL.
		await page.reload();
		await expect(page.getByTestId('filter-q')).toHaveValue('alice');

		await page.getByTestId('filter-q').fill('');
		await expect(page.locator('tr.acct-row')).toHaveCount(6);
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
		await mock(page.request, 'reset', { testDelay: 800 });
		await ready(page);
		const bob = row(page, 'bob@example.com');
		await bob.getByRole('button', { name: /^Test connection/ }).click();
		await expect(bob.getByTestId('test-state')).toContainText('Testing');
		await expect(bob.getByRole('button', { name: /^Testing/ })).toBeDisabled();
		await expect(bob.getByTestId('test-result')).toBeVisible();
		expect((await mock(page.request, 'state')).testCalls).toEqual({ 2: 1 });

		await page.reload();
		await expect(bob.getByTestId('test-result')).toContainText('IMAP');
		await bob.getByTestId('test-result').click();
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

	test('thousands of accounts: only what shows is in the DOM', async ({ page }) => {
		await mock(page.request, 'reset', { accounts: 5000 });
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toContainText('5');
		expect(await page.locator('tr.acct-row').count()).toBeLessThan(120);

		await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
		await expect(row(page, 'user05000@example.com')).toBeVisible();
		expect(await page.locator('tr.acct-row').count()).toBeLessThan(120);

		await page.getByTestId('select-all').check();
		await expect(page.getByTestId('selection-count')).toContainText(/5[\s,.  ]?000/);

		await mock(page.request, 'burst', { rounds: 3, gap: 50 });
		await expect(row(page, 'user05000@example.com').locator('[data-job-link]')).toBeVisible();
	});

	test('designed states: no server, no account, server error', async ({ page }) => {
		await mock(page.request, 'reset', { servers: 0 });
		await ready(page);
		await expect(page.getByTestId('empty-state')).toContainText('No server configured');

		await mock(page.request, 'reset', { accounts: 0 });
		await page.reload();
		await expect(page.getByTestId('empty-state')).toContainText('No accounts yet');

		await page.route('**/api/accounts', (route) => route.fulfill({ status: 500, json: { detail: 'database is locked' } }));
		await page.reload();
		await expect(page.getByRole('alert')).toContainText('database is locked');
		await page.unroute('**/api/accounts');
		await page.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('empty-state')).toContainText('No accounts yet');
	});

	test('old links still work', async ({ page }) => {
		await page.goto('/#/accounts?server=2');
		await expect(page).toHaveURL(/\/\?server=2$/);
		await expect(page.locator('tr.acct-row')).toHaveCount(2);
		await page.goto('/#/uploads');
		await expect(page).toHaveURL(/\/files$/);
		await page.goto('/#/servers');
		await expect(page).toHaveURL(/\/settings#servers$/);
	});
});
