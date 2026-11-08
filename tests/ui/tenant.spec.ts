import { expect, mock, noOverflow, ready, test } from './helpers';
import type { Page } from './helpers';

test.use({ viewport: { width: 1280, height: 900 } });

const state = (page: Page) => mock(page.request, 'state');
const pair = (page: Page, mailbox: string) => page.locator(`[data-testid="tenant-row"][data-mailbox="${mailbox}"]`);
const chip = (page: Page, name: string) => page.getByTestId(`tenant-chip-${name}`);
const MATCH = '/tenants/1/migrate?server=3';

test.describe('Microsoft 365 tenants', () => {
	test.beforeEach(async ({ signedIn, page }) => {
		void signedIn;
		await mock(page.request, 'reset', { tenant: true });
	});

	test('Servers: the tenant is listed, its test names the missing optional permission', async ({ page }) => {
		await ready(page, '/servers');
		const section = page.getByTestId('section-tenants');
		const item = section.locator('[data-source="Contoso"]');
		await expect(item).toBeVisible();
		await expect(item.getByTestId('source-verdict')).toContainText('Verified');
		await item.getByRole('button', { name: 'Test Contoso' }).click();
		const tasks = item.locator('[data-check="tasks"]');
		await expect(tasks).toBeVisible();
		await expect(tasks).toContainText('Optional');
		await expect(tasks).toContainText('Tasks.Read.All');
		expect((await state(page)).sourceTests).toHaveLength(1);
	});

	test('Servers: add a tenant, test it before saving, a wrong secret is said in plain text', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('source-name').fill('Fabrikam');
		await dialog.locator('[data-field="tenant_id"]').fill('11111111-2222-3333-4444-555555555555');
		await dialog.locator('[data-field="client_id"]').fill('66666666-7777-8888-9999-000000000000');
		await dialog.getByLabel('Client secret').fill('wrong-secret');
		await dialog.getByTestId('source-test').click();
		await expect(dialog.locator('[data-check="token"]')).toContainText('Wrong or expired client secret');
		await dialog.getByLabel('Client secret').fill('full-secret');
		await dialog.getByTestId('source-test').click();
		await expect(dialog.locator('[data-check="tasks"]')).toContainText('OK');
		await dialog.getByTestId('source-save').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await expect(page.getByTestId('section-tenants').locator('[data-source="Fabrikam"]')).toBeVisible();
		// The secret never comes back.
		const list = await (await page.request.get('/api/source-connections')).text();
		expect(list).not.toContain('full-secret');
		expect(list).not.toContain('wrong-secret');
	});

	test('Servers: a tenant with linked mailboxes cannot be deleted', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'Actions for Contoso' }).click();
		await page.getByRole('menuitem', { name: 'Delete' }).click();
		await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
		await expect(page.getByTestId('toast').filter({ hasText: 'mailboxes are linked to this tenant' })).toBeVisible();
		await expect(page.getByTestId('section-tenants').locator('[data-source="Contoso"]')).toBeVisible();
	});

	test('the row menu goes to the matching screen', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'Actions for Contoso' }).click();
		await page.getByRole('menuitem', { name: 'Migrate mailboxes' }).click();
		await expect(page).toHaveURL(/\/tenants\/1\/migrate/);
		await expect(page.getByTestId('tenant-page')).toBeVisible();
	});

	test('the New migration dialog offers the tenant and leads to the matching screen', async ({ page }) => {
		await ready(page);
		await page.getByRole('button', { name: 'New migration', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await dialog.locator('[data-kind="__tenant"]').click();
		await expect(dialog.getByTestId('tenant-pick')).toContainText('Contoso');
		await dialog.getByTestId('tenant-go').click();
		await expect(page).toHaveURL(/\/tenants\/1\/migrate/);
	});

	test('matching screen: every state, counters, filters', async ({ page }) => {
		await ready(page, MATCH);
		await expect(page.getByTestId('tenant-list')).toBeVisible();
		await expect(chip(page, 'ready')).toHaveText(/12/);
		await expect(chip(page, 'linked')).toHaveText(/2/);
		await expect(chip(page, 'no_target')).toHaveText(/1/);
		await expect(chip(page, 'no_source')).toHaveText(/2/);
		await expect(chip(page, 'other_source')).toHaveText(/1/);
		await expect(chip(page, 'conflict')).toHaveText(/1/);

		await expect(pair(page, 'camille.dupont@contoso.example')).toHaveAttribute('data-state', 'ready');
		await expect(pair(page, 'hugo.martin@contoso.example')).toHaveAttribute('data-state', 'linked');
		await expect(pair(page, 'hugo.martin@contoso.example').getByTestId('tenant-job')).toBeVisible();
		await expect(pair(page, 'nathan.petit@contoso.example')).toHaveAttribute('data-state', 'other_source');
		await expect(pair(page, 'jules.moreau@contoso.example')).toHaveAttribute('data-state', 'no_target');
		await expect(pair(page, 'theo.blanc@contoso.example')).toHaveAttribute('data-state', 'conflict');
		await expect(pair(page, 'theo.blanc@contoso.example')).toContainText('alice.martin@contoso.example');
		await expect(page.locator('[data-testid="tenant-row"][data-state="no_source"]')).toHaveCount(2);
		await expect(pair(page, 'a.laurent@contoso.example')).toContainText('via alias');
		await expect(pair(page, 'marc.fabre@contoso.example')).toContainText('Sign-in disabled');
		await expect(pair(page, 'support@contoso.example')).toContainText('Shared');

		// Only a ready or linked pair has a checkbox.
		await expect(pair(page, 'camille.dupont@contoso.example').getByTestId('tenant-tick')).toBeChecked();
		await expect(pair(page, 'nathan.petit@contoso.example').getByTestId('tenant-tick')).toHaveCount(0);
		await expect(pair(page, 'theo.blanc@contoso.example').getByTestId('tenant-tick')).toHaveCount(0);

		// Coverage: the tenant did not grant tasks, the server has DAV.
		const cov = pair(page, 'camille.dupont@contoso.example').getByTestId('tenant-coverage');
		await expect(cov.locator('[data-kind="calendar"]')).toHaveAttribute('data-cov', 'on');
		await expect(cov.locator('[data-kind="tasks"]')).toHaveAttribute('data-cov', 'no_permission');
		await expect(page.getByTestId('tenant-tasks-info')).toBeVisible();
		await expect(pair(page, 'chloe.robert@contoso.example').getByTestId('tenant-coverage').locator('[data-kind="calendar"]')).toHaveAttribute('data-cov', 'off');
		await expect(page.getByTestId('tenant-no-dav')).toHaveCount(0);
		await expect(page.getByTestId('tenant-hidden')).toContainText('1 guests');

		// A chip filters, a second click clears it.
		await chip(page, 'conflict').click();
		await expect(page.getByTestId('tenant-row')).toHaveCount(1);
		await chip(page, 'conflict').click();
		await expect(page.getByTestId('tenant-row')).not.toHaveCount(1);

		// Search.
		await page.getByTestId('tenant-q').fill('support');
		await expect(page.getByTestId('tenant-row')).toHaveCount(1);
		await page.getByTestId('tenant-q').fill('');
	});

	test('rooms and equipment stay behind their toggle', async ({ page }) => {
		await ready(page, MATCH);
		await expect(pair(page, 'room.paris@contoso.example')).toHaveCount(0);
		await page.getByRole('checkbox', { name: /Rooms and equipment/ }).check();
		await expect(pair(page, 'room.paris@contoso.example')).toHaveAttribute('data-state', 'no_target');
		await expect(pair(page, 'room.paris@contoso.example')).toContainText('Room');
		await expect(pair(page, 'projector@contoso.example')).toContainText('Equipment');
	});

	test('a server without DAV gets the loud banner', async ({ page }) => {
		await ready(page, '/tenants/1/migrate?server=2');
		const banner = page.getByTestId('tenant-no-dav');
		await expect(banner).toBeVisible();
		await expect(banner).toContainText('will NOT be migrated');
		await expect(pair(page, 'camille.dupont@contoso.example').getByTestId('tenant-coverage').locator('[data-kind="calendar"]')).toHaveAttribute('data-cov', 'no_dav');
		// The confirm dialog repeats it.
		await page.getByTestId('tenant-start').click();
		await expect(page.getByTestId('tenant-start-dialog').getByRole('alert')).toContainText('will NOT be migrated');
	});

	test('tick, start, confirm: link and start in a named batch, the list opens on it', async ({ page }) => {
		await ready(page, MATCH);
		await expect(page.getByTestId('tenant-bar-summary')).toContainText('12 selected');
		await expect(page.getByTestId('tenant-resync')).toBeDisabled();
		await page.getByTestId('tenant-start').click();
		const dialog = page.getByTestId('tenant-start-dialog');
		await expect(dialog.getByTestId('tenant-batch-name')).toHaveValue(/Contoso to Yeti \(Contoso\)/);
		await dialog.getByTestId('tenant-batch-name').fill('Contoso wave 1');
		await page.getByTestId('tenant-start-confirm').click();
		await expect(page).toHaveURL(/\/\?batch=\d+/);
		const { linkCalls } = await state(page);
		expect(linkCalls).toHaveLength(1);
		expect(linkCalls[0]).toMatchObject({ server_id: 3, start: true, batch_name: 'Contoso wave 1', mode: 'full' });
		expect(linkCalls[0].links).toHaveLength(12);
		expect(JSON.stringify(linkCalls)).not.toContain('secret');
	});

	test('tick all ready button, then untick one row', async ({ page }) => {
		await ready(page, MATCH);
		await pair(page, 'camille.dupont@contoso.example').getByTestId('tenant-tick').uncheck();
		await expect(page.getByTestId('tenant-bar-summary')).toContainText('11 selected');
		await page.getByTestId('tenant-tick-ready').click();
		await expect(page.getByTestId('tenant-bar-summary')).toContainText('12 selected');
	});

	test('final resync is only offered when every ticked row is linked', async ({ page }) => {
		await ready(page, MATCH);
		await expect(page.getByTestId('tenant-resync')).toBeDisabled();
		await chip(page, 'linked').click();
		await page.getByTestId('tenant-tick-all').check();
		await expect(page.getByTestId('tenant-bar-summary')).toContainText('2 selected');
		await expect(page.getByTestId('tenant-resync')).toBeEnabled();
		await page.getByTestId('tenant-resync').click();
		await page.getByTestId('tenant-start-confirm').click();
		await expect(page).toHaveURL(/\/\?batch=\d+/);
		const { linkCalls } = await state(page);
		expect(linkCalls[0]).toMatchObject({ mode: 'delta', start: true });
		expect(linkCalls[0].links).toHaveLength(2);
	});

	test('a tenant that refuses the listing: its message in an error panel', async ({ page }) => {
		await mock(page.request, 'reset', { tenant: true, tenantFail: 'Missing application permission User.Read.All' });
		await ready(page, MATCH);
		await expect(page.getByRole('alert').filter({ hasText: 'Missing application permission User.Read.All' })).toBeVisible();
		await expect(page.getByTestId('tenant-list')).toHaveCount(0);
	});

	test('nothing linked: the dialog stays open with the reason, the row shows its own', async ({ page }) => {
		await ready(page, MATCH);
		const row = pair(page, 'camille.dupont@contoso.example');
		const id = Number(await row.getAttribute('data-account-id'));
		await page.route('**/api/source-connections/1/link', (route) =>
			route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: { msg: 'no mailbox linked', errors: [{ account_id: id, msg: 'a job is active for this mailbox' }] } }) })
		);
		await page.getByTestId('tenant-start').click();
		await page.getByTestId('tenant-start-confirm').click();
		const dialog = page.getByTestId('tenant-start-dialog');
		await expect(dialog).toBeVisible();
		await expect(page.getByRole('dialog').getByRole('alert').filter({ hasText: 'a job is active for this mailbox' })).toBeVisible();
		await expect(row.getByTestId('tenant-row-problem')).toContainText('a job is active');
		await expect(page).toHaveURL(/\/tenants\/1\/migrate/);
	});

	test('phone: no horizontal scroll', async ({ page }) => {
		await page.setViewportSize({ width: 375, height: 800 });
		await ready(page, MATCH);
		await expect(page.getByTestId('tenant-list')).toBeVisible();
		await noOverflow(page);
	});
});
