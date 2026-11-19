import { closePanel, expect, mock, openMailbox, type Page, ready, row, test } from './helpers';

test.use({ viewport: { width: 1280, height: 800 } });

type Scenario = Record<string, unknown>;
const scenarios = (page: Page, value: Record<number, Scenario>) => mock(page.request, 'mirror', { scenarios: value });
const state = (page: Page) => mock(page.request, 'state');
const mirrorRuns = async (page: Page) => (await state(page)).runBodies.filter((b: { mode?: string }) => b.mode === 'mirror');
const lastMirrorCall = async (page: Page) => (await state(page)).mirrorCalls.at(-1);

/** A mailbox that was migrated once and has the mirror on: the only kind a mirror run accepts. */
async function migrated(page: Page, id: number, mirror = true) {
	const run = await page.request.post('/api/runs', { data: { account_ids: [id] } });
	expect(run.ok()).toBe(true);
	await expect.poll(async () => (await (await page.request.get(`/api/accounts/${id}`)).json()).job?.status, { timeout: 15_000 }).toBe('done');
	if (mirror) expect((await page.request.put(`/api/accounts/${id}`, { data: { options: { mirror: true } } })).ok()).toBe(true);
}

async function openDialog(page: Page, ...emails: string[]) {
	for (const email of emails) await row(page, email).getByRole('checkbox').check();
	await page.getByTestId('bulk-mirror').click();
	const dialog = page.getByRole('dialog', { name: 'Resync in mirror mode' });
	await expect(dialog).toBeVisible();
	await expect(dialog.getByTestId('mirror-loading')).toHaveCount(0);
	return dialog;
}

const ALICE = 'alice@example.com';
const BOB = 'bob@example.com';

test.describe('mirror mode', () => {
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('the switch of a mailbox saves at once and explains itself', async ({ page }) => {
		await ready(page);
		const settings = await openMailbox(page, ALICE);
		const toggle = settings.getByRole('checkbox', { name: `Mirror mode for ${ALICE}` });
		await expect(toggle).not.toBeChecked();
		// The resync action only shows once the mirror is on.
		await expect(settings.getByTestId('account-mirror-resync')).toHaveCount(0);

		await settings.getByTestId('step-mirror').getByRole('button', { name: /more information/i }).hover();
		await expect(page.getByRole('tooltip')).toContainText('The source is never modified.');

		await toggle.check();
		await expect.poll(async () => (await state(page)).accounts.find((a: { id: number }) => a.id === 1).options.mirror).toBe(true);
		await expect(settings.getByTestId('account-mirror-resync')).toBeVisible();
		await toggle.uncheck();
		await expect.poll(async () => (await state(page)).accounts.find((a: { id: number }) => a.id === 1).options.mirror).toBe(false);
		await closePanel(page);
	});

	test('the bulk edit turns the mirror on for the whole selection and leaves it alone otherwise', async ({ page }) => {
		await ready(page);
		await row(page, ALICE).getByRole('checkbox').check();
		await row(page, BOB).getByRole('checkbox').check();
		await page.getByTestId('bulk-edit').click();
		const dialog = page.getByRole('dialog', { name: 'Edit 2 mailboxes' });
		// Nothing chosen: nothing to apply.
		await expect(dialog.getByRole('button', { name: 'Apply to 2' })).toBeDisabled();
		await dialog.getByTestId('bulk-mirror-select').selectOption('on');
		await dialog.getByRole('button', { name: 'Apply to 2' }).click();
		await expect(page.getByTestId('toast').first()).toContainText('2');
		const accounts = (await state(page)).accounts;
		expect(accounts.filter((a: { options: { mirror: boolean } }) => a.options.mirror).map((a: { id: number }) => a.id)).toEqual([1, 2]);
		expect(accounts.find((a: { id: number }) => a.id === 1).options.calendar).toBe(true);
	});

	test('the comparison writes nothing, shows the counts and the sample, and a deletion is confirmed with its number', async ({ page }) => {
		await migrated(page, 1);
		await ready(page);
		const dialog = await openDialog(page, ALICE);

		// A dry run: it asked for it, and no job was created.
		const call = await lastMirrorCall(page);
		expect(call).toMatchObject({ account_ids: [1], mode: 'mirror', dry_run: true, max_delete_percent: 20, allow_large_delete: false });
		expect(await mirrorRuns(page)).toEqual([]);

		await expect(dialog.getByTestId('mirror-readonly')).toHaveText('The source is never modified');
		await expect(dialog.getByTestId('mirror-totals')).toHaveText('13 to add, 3 to update, 7 to delete');
		const mail = dialog.getByTestId('mirror-row-mail').locator('td');
		await expect(mail.nth(1)).toHaveText('12');
		await expect(mail.nth(2)).toHaveText('3');
		await expect(mail.nth(3)).toHaveText('5');
		await expect(dialog.getByTestId('mirror-row-contacts').locator('td').nth(3)).toHaveText('2');
		await expect(dialog.getByTestId('mirror-row-tasks')).toHaveCount(0);

		// 23 changes, 20 lines, the deletions first.
		await dialog.getByTestId('mirror-sample').locator('summary').click();
		await expect(dialog.getByTestId('mirror-sample-line')).toHaveCount(20);
		await expect(dialog.getByTestId('mirror-sample-line').first()).toContainText('Removed at source');
		await expect(dialog.getByTestId('mirror-sample')).toContainText('Sample: 20 of 23 changes');

		const apply = dialog.getByTestId('mirror-apply');
		await expect(apply).toBeEnabled();
		await expect(dialog.getByTestId('mirror-apply-reason')).toHaveCount(0);
		await apply.click();
		const confirm = page.getByRole('dialog', { name: 'Delete 7 items at the destination' });
		await expect(confirm).toContainText('7 items');
		// Cancel: nothing started.
		await confirm.getByRole('button', { name: 'Cancel' }).click();
		expect(await mirrorRuns(page)).toEqual([]);

		await apply.click();
		await page.getByRole('dialog', { name: 'Delete 7 items at the destination' }).getByRole('button', { name: 'Delete 7 and apply' }).click();
		await expect(page.getByTestId('toast').first()).toContainText('Mirror queued for 1 mailboxes');
		await expect(page.getByRole('dialog')).toHaveCount(0);
		const bodies = await mirrorRuns(page);
		expect(bodies.at(-1)).toMatchObject({ account_ids: [1], mode: 'mirror', dry_run: false, max_delete_percent: 20, allow_large_delete: false });
		expect(await (await page.request.get('/api/jobs')).json()).toEqual(expect.arrayContaining([expect.objectContaining({ account_id: 1, mode: 'mirror' })]));
	});

	test('what the run did shows in the report of the job, and only for a mirror run', async ({ page }) => {
		await migrated(page, 1);
		await scenarios(page, { 1: { add: { mail: 4 }, update: { mail: 1 }, delete: { mail: 3 }, skipped: { mail: 6 }, errors: { contacts: 1 } } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);
		await dialog.getByTestId('mirror-apply').click();
		await page.getByRole('dialog', { name: 'Delete 3 items at the destination' }).getByRole('button', { name: 'Delete 3 and apply' }).click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		// The run took its mailboxes out of the selection.
		await expect(page.getByTestId('selection-bar')).toHaveCount(0);

		const jobs = await (await page.request.get('/api/jobs')).json();
		const mirrorJob = jobs.find((j: { mode: string }) => j.mode === 'mirror');
		await expect.poll(async () => (await (await page.request.get(`/api/jobs/${mirrorJob.id}`)).json()).status, { timeout: 15_000 }).toBe('done');
		await ready(page, `/jobs/${mirrorJob.id}?tab=report`);
		const report = page.getByTestId('report-mirror');
		await expect(report).toBeVisible();
		await expect(report.getByRole('heading', { name: 'Mirror' })).toBeVisible();
		const mail = report.getByTestId('report-mirror-mail').locator('td');
		await expect(mail.nth(1)).toHaveText('4');
		await expect(mail.nth(2)).toHaveText('1');
		await expect(mail.nth(3)).toHaveText('3');
		await expect(mail.nth(4)).toHaveText('6');
		await expect(report.getByTestId('report-mirror-contacts').locator('td').nth(5)).toHaveText('1');
		await expect(report.getByTestId('report-mirror-skipped')).toContainText('6 deletions were skipped');

		// An ordinary job has no such section.
		await migrated(page, 2, false);
		const plain = (await (await page.request.get('/api/accounts/2')).json()).job;
		await ready(page, `/jobs/${plain.id}?tab=report`);
		await expect(page.getByTestId('job-report')).toBeVisible();
		await expect(page.getByTestId('report-mirror')).toHaveCount(0);
	});

	test('a mailbox the server blocks says why and how to fix it, and stays out of the run', async ({ page }) => {
		await migrated(page, 1);
		await ready(page);
		const dialog = await openDialog(page, ALICE, BOB);

		// Bob never ran and has no mirror: two blocks, each with its fix.
		await expect(dialog.getByTestId('mirror-blocked-count')).toHaveText('1 mailboxes are blocked and will not run');
		const off = dialog.getByTestId('mirror-block-mirror_off');
		await expect(off).toContainText('Mirror mode is off for this mailbox');
		await expect(off).toContainText('Turn it on, then compare again.');
		await expect(dialog.getByTestId('mirror-block-no_prior_run')).toContainText('Run a normal migration first');
		await expect(dialog.getByTestId('mirror-account').filter({ hasText: BOB })).toContainText('Blocked');
		await expect(dialog.getByTestId('mirror-account').filter({ hasText: ALICE })).toContainText('Ready');
		// Only Alice counts.
		await expect(dialog.getByTestId('mirror-totals')).toHaveText('13 to add, 3 to update, 7 to delete');

		await dialog.getByTestId('mirror-enable').click();
		await expect(page.getByTestId('toast').first()).toContainText('Mirror turned on');
		await expect(dialog.getByTestId('mirror-block-mirror_off')).toHaveCount(0);
		await expect(dialog.getByTestId('mirror-block-no_prior_run')).toBeVisible();
		expect((await state(page)).accounts.find((a: { id: number }) => a.id === 2).options.mirror).toBe(true);

		await dialog.getByTestId('mirror-apply').click();
		await page.getByRole('dialog', { name: 'Delete 7 items at the destination' }).getByRole('button', { name: 'Delete 7 and apply' }).click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		// Alice only.
		expect((await mirrorRuns(page)).at(-1)).toMatchObject({ account_ids: [1], dry_run: false });
	});

	test('with every mailbox blocked, Apply is off and says why', async ({ page }) => {
		await ready(page);
		const dialog = await openDialog(page, BOB);
		const apply = dialog.getByTestId('mirror-apply');
		await expect(apply).toBeDisabled();
		await expect(dialog.getByTestId('mirror-apply-reason')).toHaveText('No mailbox can run until the blocks above are fixed');
		await expect(apply).toHaveAttribute('title', 'No mailbox can run until the blocks above are fixed');
		await expect(dialog.getByTestId('mirror-readonly')).toBeVisible();
	});

	test('a destination that already matches has nothing to apply', async ({ page }) => {
		await migrated(page, 1);
		await scenarios(page, { 1: { add: {}, update: {}, delete: {} } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);
		await expect(dialog.getByTestId('mirror-totals')).toHaveText('0 to add, 0 to update, 0 to delete');
		await expect(dialog.getByTestId('mirror-apply')).toBeDisabled();
		await expect(dialog.getByTestId('mirror-apply-reason')).toHaveText('Nothing to apply: the destination already matches the source');
	});

	test('additions alone are applied without a confirmation, with a neutral button', async ({ page }) => {
		await migrated(page, 1);
		await scenarios(page, { 1: { add: { mail: 2 }, update: {}, delete: {} } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);
		await expect(dialog.getByTestId('mirror-apply')).not.toHaveClass(/text-warning-ink/);
		await dialog.getByTestId('mirror-apply').click();
		await expect(page.getByTestId('toast').first()).toContainText('Mirror queued');
		await expect(page.getByRole('dialog')).toHaveCount(0);
	});

	test('a deletion over the safety limit is refused; the limit or the large deletion lets it through', async ({ page }) => {
		await migrated(page, 1);
		// 60 of 200 items: 30%, the limit is 20%.
		await scenarios(page, { 1: { add: {}, update: {}, delete: { mail: 60 }, holds: 200 } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);

		const block = dialog.getByTestId('mirror-block-too_many_deletes');
		await expect(block).toContainText('The run would delete more than the safety limit allows');
		await expect(block).toContainText('limit is 20%');
		await expect(dialog.getByTestId('mirror-apply')).toBeDisabled();
		// The limit is open, and the large deletion is offered with its warning.
		const percent = dialog.getByTestId('mirror-percent');
		await expect(percent).toBeVisible();
		await expect(percent).toHaveValue('20');
		const large = dialog.getByTestId('mirror-large');
		await expect(large).toContainText('Not recommended');
		await expect(large.getByRole('checkbox')).not.toBeChecked();

		// A higher limit.
		await percent.fill('40');
		await percent.blur();
		await expect(dialog.getByTestId('mirror-block-too_many_deletes')).toHaveCount(0);
		expect(await lastMirrorCall(page)).toMatchObject({ max_delete_percent: 40, allow_large_delete: false });
		await expect(dialog.getByTestId('mirror-apply')).toBeEnabled();
		await expect(dialog.getByTestId('mirror-large')).toHaveCount(0);

		// Back to the limit, then the large deletion instead.
		await percent.fill('20');
		await percent.blur();
		await expect(dialog.getByTestId('mirror-block-too_many_deletes')).toBeVisible();
		await dialog.getByTestId('mirror-large-allow').check();
		await expect(dialog.getByTestId('mirror-block-too_many_deletes')).toHaveCount(0);
		expect(await lastMirrorCall(page)).toMatchObject({ max_delete_percent: 20, allow_large_delete: true });
		// The box stays, to be unticked.
		await expect(dialog.getByTestId('mirror-large-allow')).toBeChecked();

		await dialog.getByTestId('mirror-apply').click();
		await page.getByRole('dialog', { name: 'Delete 60 items at the destination' }).getByRole('button', { name: 'Delete 60 and apply' }).click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		expect((await mirrorRuns(page)).at(-1)).toMatchObject({ account_ids: [1], dry_run: false, allow_large_delete: true });
	});

	test('the large deletion is not offered for another block, and a percentage out of range is brought back', async ({ page }) => {
		await migrated(page, 1);
		await scenarios(page, { 1: { block: 'source_empty' } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);
		await expect(dialog.getByTestId('mirror-block-source_empty')).toContainText('The source looks empty');
		await expect(dialog.getByTestId('mirror-large')).toHaveCount(0);
		await dialog.getByTestId('mirror-limit').locator('summary').click();
		const percent = dialog.getByTestId('mirror-percent');
		await percent.fill('500');
		await percent.blur();
		await expect(percent).toHaveValue('100');
		expect(await lastMirrorCall(page)).toMatchObject({ max_delete_percent: 100 });
	});

	test('a failed comparison shows its error and can be tried again', async ({ page }) => {
		await migrated(page, 1);
		await scenarios(page, { 1: { fail: 'source refused the listing' } });
		await ready(page);
		const dialog = await openDialog(page, ALICE);
		await expect(dialog.getByRole('alert')).toContainText('The comparison failed');
		await expect(dialog.getByRole('alert')).toContainText('source refused the listing');
		await expect(dialog.getByTestId('mirror-apply')).toBeDisabled();
		await scenarios(page, {});
		await dialog.getByRole('button', { name: 'Retry' }).click();
		await expect(dialog.getByTestId('mirror-totals')).toBeVisible();
		await expect(dialog.getByTestId('mirror-apply')).toBeEnabled();
	});

	test('the panels of a mailbox and of a finished job open the same dialog', async ({ page }) => {
		await migrated(page, 1);
		await ready(page);

		// The panel of the job: the action is there for a finished job.
		await row(page, ALICE).locator('[data-job-link]').click();
		const panel = page.getByTestId('job-panel');
		await panel.getByTestId('job-mirror').click();
		const dialog = page.getByRole('dialog', { name: 'Resync in mirror mode' });
		await expect(dialog.getByTestId('mirror-totals')).toBeVisible();
		expect((await lastMirrorCall(page)).account_ids).toEqual([1]);
		await dialog.getByRole('button', { name: 'Cancel' }).click();
		await expect(dialog).toHaveCount(0);
		// Once, not twice, in a job's panel.
		await expect(panel.getByRole('button', { name: 'Resync in mirror mode' })).toHaveCount(1);
		await closePanel(page);

		// The panel of a mailbox with no job yet (Carol): the switch, then the action.
		const settings = await openMailbox(page, 'carol@example.com');
		await settings.getByRole('checkbox', { name: 'Mirror mode for carol@example.com' }).check();
		await settings.getByTestId('account-mirror-resync').click();
		await expect(page.getByRole('dialog', { name: 'Resync in mirror mode' }).getByTestId('mirror-block-no_prior_run')).toBeVisible();
	});

	test('narrow screen: the dialog does not overflow', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 800 });
		await migrated(page, 1);
		await ready(page);
		await row(page, ALICE).getByRole('checkbox').check();
		await page.getByTestId('bulk-mirror').click();
		const dialog = page.getByRole('dialog', { name: 'Resync in mirror mode' });
		await expect(dialog.getByTestId('mirror-totals')).toBeVisible();
		const box = await dialog.boundingBox();
		expect(box!.width).toBeLessThanOrEqual(390.5);
		const wide = await dialog.evaluate((el) => el.scrollWidth > el.clientWidth);
		expect(wide).toBe(false);
	});
});
