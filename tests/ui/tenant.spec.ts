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
		await dialog.getByText('Enter the app details by hand').click();
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

	test('Connect with Microsoft: code, live steps, check list, then the matching screen', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('provision-connect')).toBeVisible();
		// The manual fields are folded away.
		await expect(dialog.locator('[data-field="tenant_id"]')).toBeHidden();
		await dialog.getByTestId('source-name').fill('Fabrikam');
		await dialog.getByTestId('provision-connect').click();
		await expect(dialog.getByTestId('provision-code')).toHaveText('ABCD-EFGH');
		await expect(dialog.getByTestId('provision-notice')).toContainText('Microsoft Graph Command Line Tools');
		await expect(dialog.getByTestId('provision-steps').locator('[data-step="signin"]')).toHaveAttribute('data-state', 'current');
		await page.context().route('https://microsoft.com/**', (route) => route.fulfill({ status: 200, contentType: 'text/html', body: '<title>sign-in</title>' }));
		const popup = page.waitForEvent('popup');
		await dialog.getByTestId('provision-open').click();
		const tab = await popup;
		await tab.waitForLoadState();
		expect(tab.url()).toContain('microsoft.com/devicelogin');
		await expect(dialog.locator('[data-step="propagation"][data-state="current"]')).toBeVisible({ timeout: 20_000 });
		await expect(dialog.locator('[data-step="signin"]')).toHaveAttribute('data-state', 'done');
		await expect(dialog.getByTestId('provision-done')).toBeVisible({ timeout: 20_000 });
		await expect(dialog.locator('[data-check="tasks"]')).toContainText('OK');
		expect((await state(page)).provisionCalls).toEqual([{ cloud: 'global', name: 'Fabrikam' }]);
		await dialog.getByTestId('provision-migrate').click();
		await expect(page).toHaveURL(/\/tenants\/2\/migrate/);
	});

	test('Connect with Microsoft: an error is said, Retry restarts, Enter by hand opens the fields', async ({ page }) => {
		await mock(page.request, 'provision', { fail: 'admin' });
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('provision-connect').click();
		// a translated sentence from error_code, the server's text below as the details
		await expect(dialog.getByTestId('error-summary')).toContainText('not an administrator', { timeout: 20_000 });
		await expect(dialog.getByTestId('provision-panel')).toHaveAttribute('data-error-code', 'not_admin');
		await expect(dialog.getByTestId('error-details')).toContainText('Graph 403 Authorization_RequestDenied');
		await mock(page.request, 'provision', { fail: null });
		await dialog.getByTestId('provision-retry').click();
		await expect(dialog.getByTestId('provision-code')).toBeVisible();
		await dialog.getByTestId('provision-cancel').click();
		await expect(dialog.getByTestId('provision-connect')).toBeVisible();
		await mock(page.request, 'provision', { fail: 'expired' });
		await dialog.getByTestId('provision-connect').click();
		await expect(dialog.getByTestId('error-summary')).toHaveText('The code expired. Start again.', { timeout: 20_000 });
		await dialog.getByTestId('provision-manual').click();
		await expect(dialog.locator('[data-field="tenant_id"]')).toBeVisible();
		await expect(dialog.getByTestId('provision-panel')).toHaveCount(0);
	});

	test('Connect with Microsoft: a flow lost to a restart (404) or no answer at all says mccb no longer responds', async ({ page }) => {
		await mock(page.request, 'provision', { fail: 'lost' });
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('provision-connect').click();
		await expect(dialog.getByTestId('error-summary')).toHaveText('mccb no longer responds (restarted?). Start again.', { timeout: 20_000 });
		await expect(dialog.getByTestId('error-details')).toHaveCount(0);
		await mock(page.request, 'provision', { fail: null });
		await page.route('**/api/source-connections/provision/*', (route) => (route.request().method() === 'GET' ? route.abort('connectionrefused') : route.fallback()));
		await dialog.getByTestId('provision-retry').click();
		await expect(dialog.getByTestId('provision-code')).toBeVisible();
		await expect(dialog.getByTestId('error-summary')).toHaveText('mccb no longer responds (restarted?). Start again.', { timeout: 20_000 });
		await expect(dialog.getByRole('alert')).not.toContainText('Failed to fetch');
	});

	test('Connect with Microsoft: permissions still propagating is a warning with Test again, not Retry', async ({ page }) => {
		await mock(page.request, 'provision', { fail: 'propagation' });
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await dialog.getByTestId('provision-connect').click();
		await expect(dialog.getByTestId('provision-pending')).toContainText('still activating', { timeout: 20_000 });
		await expect(dialog.getByTestId('provision-pending-details')).toContainText('run Test again later');
		await expect(dialog.getByTestId('provision-retry')).toHaveCount(0);
		await expect(dialog.locator('[data-check="token"]')).toBeVisible();
		await dialog.getByTestId('provision-test-again').click();
		await expect(dialog.locator('[data-check="tasks"]')).toContainText('OK');
	});

	test('Connect with Microsoft is hidden when the server has no bootstrap app', async ({ page }) => {
		await mock(page.request, 'reset', { tenant: true, provision: false });
		await ready(page, '/servers');
		await page.getByTestId('source-add').click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('provision-connect')).toHaveCount(0);
		await expect(dialog.locator('[data-field="tenant_id"]')).toBeVisible();
	});

	test('the tenant dialog says its field labels in the language of the UI', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'Actions for Contoso' }).click();
		await page.getByRole('menuitem', { name: 'Edit' }).click();
		await expect(page.getByRole('dialog')).toContainText('Directory (tenant) ID of the Entra application.');
	});

	test('a tenant made by Connect with Microsoft offers to remove the app from the tenant', async ({ page }) => {
		await ready(page, '/servers');
		await page.getByRole('button', { name: 'Actions for Contoso' }).click();
		await expect(page.getByRole('menuitem', { name: 'Remove the app from the tenant' })).toHaveCount(0);
		await page.keyboard.press('Escape');
		await page.getByTestId('source-add').click();
		let dialog = page.getByRole('dialog');
		await dialog.getByTestId('provision-connect').click();
		await expect(dialog.getByTestId('provision-done')).toBeVisible({ timeout: 20_000 });
		await dialog.getByTestId('provision-close').click();
		await expect(page.getByRole('dialog')).toHaveCount(0);
		await page.getByRole('button', { name: 'Actions for Fabrikam' }).click();
		await page.getByRole('menuitem', { name: 'Remove the app from the tenant' }).click();
		dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('provision-code')).toBeVisible();
		await expect(dialog.locator('[data-step="remove"]')).toBeVisible();
		await expect(dialog.getByTestId('provision-done')).toContainText('removed', { timeout: 20_000 });
		await dialog.getByTestId('provision-close').click();
		await page.getByRole('button', { name: 'Actions for Fabrikam' }).click();
		await expect(page.getByRole('menuitem', { name: 'Remove the app from the tenant' })).toHaveCount(0);
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

	test('Servers: DAV answers while switched off -> offered, one click enables it', async ({ page }) => {
		const made = await page.request.post('/api/servers', { data: { name: 'Yety', imap_host: 'dav.yety.test', dav_enabled: false } });
		expect(made.ok()).toBeTruthy();
		await ready(page, '/servers');
		const row = page.locator('[data-server="Yety"]');
		const found = row.getByTestId('server-dav-found');
		await expect(found).toBeVisible();
		await row.getByTestId('server-enable-dav').click();
		await expect(found).toHaveCount(0);
		await expect(row).toContainText('CalDAV and CardDAV enabled');
		const servers = await (await page.request.get('/api/servers')).json();
		expect(servers.find((x: { name: string }) => x.name === 'Yety').dav_enabled).toBe(true);
	});

	test('phone: no horizontal scroll', async ({ page }) => {
		await page.setViewportSize({ width: 375, height: 800 });
		await ready(page, MATCH);
		await expect(page.getByTestId('tenant-list')).toBeVisible();
		await noOverflow(page);
	});
});
