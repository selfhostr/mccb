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
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
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
		const select = settings.getByRole('combobox', { name: 'PST file for alice@example.com' });
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
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
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
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
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

		// The mailboxes without a source have a chip of their own: nothing to open first.
		await expect(page.getByTestId('filter-status')).toHaveCount(0);
		await expect(page.getByTestId('chip-no_pst')).toContainText('3');
		await page.getByTestId('chip-no_pst').click();
		await expect(page).toHaveURL(/status=no_pst/);
		await expect(page.getByTestId('chip-no_pst')).toHaveAttribute('aria-pressed', 'true');
		await expect(rows(page)).toHaveCount(3);
		await page.reload();
		await expect(page.getByTestId('chip-no_pst')).toHaveAttribute('aria-pressed', 'true');
		await expect(rows(page)).toHaveCount(3);

		// The narrower states (not tested, tested ok...) wait behind one button, on the same filter.
		await page.getByTestId('filter-more').click();
		await expect(page.getByTestId('filter-status')).toHaveValue('no_pst');
		await page.getByTestId('filter-status').selectOption('untested');
		await expect(page).toHaveURL(/status=untested/);
		await expect(page.getByTestId('chip-no_pst')).toHaveAttribute('aria-pressed', 'false');
	});

	test('the sentence says how far it is, and the bar moves with it', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		await expect(page.getByTestId('page-sentence')).toHaveText('0 of 6 mailboxes done.');
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
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
		// aria-disabled, not disabled: the item stays reachable.
		await expect(page.getByRole('menuitem', { name: 'Testing' })).toHaveAttribute('aria-disabled', 'true');
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
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
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

	test('a new migration is one form between two servers, and its panel is ready to start', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		const dialog = page.getByRole('dialog');
		// Servers only: a file is migrated from the Files tab.
		await expect(dialog.getByRole('radiogroup', { name: 'From' })).toBeVisible();
		await expect(dialog.getByRole('radiogroup', { name: 'Destination server' })).toBeVisible();
		await expect(dialog.getByTestId('new-source')).toHaveCount(0);
		await dialog.getByRole('textbox', { name: 'Source login' }).fill('new.one@legacy.example');
		await dialog.getByLabel('Source password').fill('old-pw');
		await dialog.getByRole('textbox', { name: 'Destination address' }).fill('new.one@example.com');
		await dialog.getByLabel('Destination password').fill('pw');
		await dialog.getByTestId('new-submit').click();
		await expect(dialog).toHaveCount(0);

		// One mailbox goes on in its own panel, with its source already set from the form.
		const panel = page.getByTestId('job-panel');
		await expect(panel).toContainText('new.one@example.com');
		await expect(panel.getByTestId('source-type')).toHaveValue('server-1');
		await expect(panel.getByTestId('account-start')).toBeEnabled();
		await expect(row(page, 'new.one@example.com')).not.toContainText('No source yet');

		await panel.getByTestId('account-start').click();
		const confirm = page.getByRole('dialog');
		await expect(confirm).toContainText('Ready to start: 1');
		// The batch name and the mode are in the dialog itself, not behind a disclosure.
		await confirm.getByTestId('import-confirm').click();
		await expect(page.getByTestId('toast').filter({ hasText: 'Jobs queued: 1' })).toBeVisible();
		await expect(panel.getByTestId('account-start')).toHaveCount(0);
	});

	test('several new mailboxes come back selected, ready to be tested and started', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('new-row-add').click();
		const rowsIn = dialog.getByTestId('new-row');
		await expect(rowsIn).toHaveCount(2);
		for (const [i, name] of ['one', 'two'].entries()) {
			const line = rowsIn.nth(i);
			await line.getByRole('textbox', { name: 'Source login' }).fill(`${name}@legacy.example`);
			await line.getByLabel('Source password').fill('old-pw');
			await line.getByRole('textbox', { name: 'Destination address' }).fill(`${name}.new@example.com`);
			await line.getByLabel('Destination password').fill('pw');
		}
		await expect(dialog.getByTestId('new-submit')).toHaveText('Add 2 mailboxes');
		await dialog.getByTestId('new-submit').click();
		await expect(dialog).toHaveCount(0);
		await expect(page.getByTestId('toast').filter({ hasText: '2 mailboxes added and selected' })).toBeVisible();

		// No panel opens: the new mailboxes are the selection, and the bar offers the next step.
		await expect(page.getByTestId('job-panel')).toHaveCount(0);
		await expect(page.getByTestId('selection-count')).toContainText('2');
		await expect(row(page, 'one.new@example.com').getByRole('checkbox')).toBeChecked();
		await expect(row(page, 'two.new@example.com').getByRole('checkbox')).toBeChecked();
		await expect(page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true })).toBeEnabled();
	});

	test('a mailbox without a source says so, and starts once a file is chosen', async ({ page }) => {
		await ready(page);
		await expect(row(page, 'dave@example.com')).toContainText('No source yet');
		const settings = await openMailbox(page, 'dave@example.com');
		const panel = page.getByTestId('job-panel');
		await expect(panel.getByTestId('account-start')).toBeDisabled();
		await expect(panel.getByTestId('start-hint')).toHaveText('Choose where the mail comes from first');

		await settings.getByRole('combobox', { name: /^PST file for/ }).selectOption({ index: 1 });
		await expect(panel.getByTestId('account-start')).toBeEnabled();
		await panel.getByTestId('account-start').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Ready to start: 1');
		await dialog.getByTestId('import-confirm').click();
		await expect(page.getByTestId('toast').filter({ hasText: 'Jobs queued: 1' })).toBeVisible();
	});

	test('the selection bar edits, deletes and runs jobs for the selection', async ({ page }) => {
		await mock(page.request, 'reset', { hold: true });
		await ready(page);
		const bar = page.getByTestId('selection-bar');
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await expect(bar.getByTestId('bulk-edit')).toBeEnabled();
		await expect(bar.getByTestId('bulk-delete')).toBeEnabled();
		// No job yet: nothing to pause, resume, stop or retry.
		await expect(bar.getByRole('button', { name: 'Pause' })).toHaveCount(0);
		await expect(bar.getByRole('button', { name: 'Stop' })).toHaveCount(0);

		await bar.getByRole('button', { name: 'Start', exact: true }).click();
		await page.getByTestId('import-confirm').click();
		await expect(page.getByTestId('toast').first()).toContainText('2');
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();

		// Both jobs are queued: they can be paused or stopped, and only a paused one resumed.
		await expect(bar.getByTestId('bulk-pause')).toBeVisible();
		await expect(bar.getByTestId('bulk-stop')).toBeVisible();
		await expect(bar.getByTestId('bulk-resume')).toHaveCount(0);
		await bar.getByTestId('bulk-pause').click();
		await expect(page.getByTestId('toast').filter({ hasText: '2 jobs updated' })).toBeVisible();
		await expect(bar.getByTestId('bulk-resume')).toBeVisible();
		await expect(bar.getByTestId('bulk-pause')).toHaveCount(0);

		// Stopping asks first.
		await bar.getByTestId('bulk-stop').click();
		const confirm = page.getByRole('dialog');
		await expect(confirm).toContainText('Stop the selected jobs');
		await confirm.getByRole('button', { name: 'Stop', exact: true }).click();
		await expect(confirm).toHaveCount(0);
		await expect(bar.getByTestId('bulk-retry-errors')).toBeVisible();
		await expect(bar.getByTestId('bulk-stop')).toHaveCount(0);

		// Deleting asks first too, and says how many.
		await bar.getByTestId('bulk-delete').click();
		const del = page.getByRole('dialog');
		await expect(del).toContainText('Delete the 2 selected mailboxes?');
		await del.getByRole('button', { name: 'Delete 2' }).click();
		await expect(page.getByTestId('toast').filter({ hasText: '2 mailboxes deleted' })).toBeVisible();
		await expect(rows(page)).toHaveCount(4);
		await expect(page.getByTestId('selection-bar')).toHaveCount(0);
	});

	/** alice and bob can start, dave has no source, and alice's last connection test was refused. */
	async function openMixedImport(page: import('./helpers').Page) {
		await ready(page);
		const alice = await (await page.request.get('/api/accounts/1')).json();
		await mock(page.request, 'event', { kind: 'account', data: { ...alice, status: 'auth_failed' } });
		await expect(row(page, 'alice@example.com')).toContainText('Login refused');
		for (const email of ['alice@example.com', 'bob@example.com', 'dave@example.com']) await row(page, email).getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Start the migration');
		return dialog;
	}

	test('the start says what will not start: no source, and a connection refused at the last test', async ({ page }) => {
		const dialog = await openMixedImport(page);
		await expect(dialog).toContainText('Ready to start: 1');
		await expect(dialog).toContainText('No source set, will not start: 1');
		await expect(dialog.getByTestId('import-pst-list')).toContainText('dave@example.com');
		await expect(dialog).toContainText('Connection refused at the last test, will not start: 1');
		await expect(dialog.getByTestId('import-refused-list')).toContainText('alice@example.com');
		// The batch name and the mode are not behind a disclosure any more.
		await expect(dialog.getByTestId('import-batch')).toBeVisible();
		await expect(dialog.getByTestId('import-options')).toHaveCount(0);

		// Keep only the ready ones: the other mailboxes leave the selection and the list.
		await dialog.getByTestId('import-keep-ready').click();
		await expect(dialog).not.toContainText('will not start');
		await dialog.getByTestId('import-confirm').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await mock(page.request, 'state')).runCalls).toEqual([[2]]);
		await expect(page.getByTestId('selection-count')).toHaveCount(0);
	});

	test('the start warns that calendars and contacts will not come when the server has DAV off, and one click turns it on', async ({ page }) => {
		await ready(page);
		const { id: _id, last_test: _t, last_test_result: _r, ...server } = await (await page.request.get('/api/servers/1')).json();
		await page.request.put('/api/servers/1', { data: { ...server, dav_enabled: false } });
		await ready(page);
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Start', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('nodav-notice')).toContainText('Calendars and contacts will not be migrated: CalDAV/CardDAV is disabled on Production.');
		await expect(dialog.getByTestId('import-nodav-list')).toContainText('bob@example.com');
		// The start is not blocked.
		await expect(dialog.getByTestId('import-confirm')).toBeEnabled();
		await dialog.getByTestId('nodav-enable').click();
		await expect(dialog.getByTestId('nodav-notice')).toHaveCount(0);
		expect((await (await page.request.get('/api/servers/1')).json()).dav_enabled).toBe(true);
	});

	test('a refused connection can be started anyway, from a checkbox', async ({ page }) => {
		const dialog = await openMixedImport(page);
		const include = dialog.getByTestId('import-refused-include');
		await expect(include).not.toBeChecked();
		await expect(dialog).toContainText('Ready to start: 1');
		await include.check();
		await expect(dialog).toContainText('Ready to start: 2');
		// Two mailboxes make a batch by default, named after the day.
		await expect(dialog.getByTestId('import-batch')).toHaveValue(/^Migration of \d{2}\/\d{2}$/);
		await dialog.getByTestId('import-confirm').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		const state = await mock(page.request, 'state');
		expect([...state.runCalls[0]].sort()).toEqual([1, 2]);
	});

	test('editing several mailboxes at once changes only what is filled in', async ({ page }) => {
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await page.getByTestId('bulk-edit').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toContainText('Edit 2 mailboxes');
		// Nothing is filled in: nothing to apply.
		await expect(dialog.getByRole('button', { name: 'Apply to 2' })).toBeDisabled();
		await dialog.getByLabel('Calendar').selectOption('off');
		await dialog.getByRole('button', { name: 'Apply to 2' }).click();
		await expect(page.getByTestId('toast').filter({ hasText: '2 mailboxes changed' })).toBeVisible();
		await expect(dialog).toHaveCount(0);
		const { accounts } = await mock(page.request, 'state');
		const calendar = (id: number) => accounts.find((a: { id: number }) => a.id === id).options.calendar;
		expect([calendar(1), calendar(2), calendar(3)]).toEqual([false, false, true]);
	});

	test('turning calendars on for several mailboxes says whose source has no CalDAV, and one click on its server fixes it', async ({ page }) => {
		await ready(page);
		// alice reads her old mail server, saved as Staging, whose DAV is off; bob comes from a PST.
		const res = await page.request.put('/api/accounts/1/source', { data: { kind: 'imap', config: { host: 'mail.home.arpa', port: 143, security: 'starttls', login: 'alice@example.com' }, secrets: { password: 'pw' } } });
		expect(res.ok()).toBe(true);
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await row(page, 'bob@example.com').getByRole('checkbox').check();
		await page.getByTestId('bulk-edit').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('srcdav-notice')).toHaveCount(0);
		await dialog.getByLabel('Calendar').selectOption('on');
		await expect(dialog.getByTestId('srcdav-notice')).toContainText('Calendars and contacts will stay at the source: CalDAV/CardDAV is off on the source server Staging.');
		await expect(dialog.getByTestId('srcdav-list')).toHaveText('alice@example.com');
		await dialog.getByTestId('srcdav-enable').click();
		await expect(dialog.getByTestId('srcdav-notice')).toHaveCount(0);
		expect((await (await page.request.get('/api/servers/2')).json()).dav_enabled).toBe(true);
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
		await expect(page.getByTestId('job-panel').getByRole('combobox', { name: 'PST file for alice@example.com' })).toBeVisible();
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
		// The card carries its own "New migration" button, next to the one of the header.
		await expect(page.getByTestId('empty-state').getByRole('button', { name: 'New migration', exact: true })).toBeVisible();

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
