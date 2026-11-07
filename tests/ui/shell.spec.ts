import { expect, noOverflow, openMailbox, ready, row, test } from './helpers';

test.describe('at 390 px', () => {
	test.use({ viewport: { width: 390, height: 844 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	for (const url of ['/', '/files', '/servers', '/settings']) {
		test(`no horizontal overflow on ${url}`, async ({ page }) => {
			await ready(page, url);
			await noOverflow(page);
		});
	}

	test('no overflow with the selection bar, the job panel and a dialog', async ({ page }) => {
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		await expect(page.getByTestId('selection-bar')).toBeVisible();
		await noOverflow(page);

		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await noOverflow(page);
		await page.getByTestId('import-confirm').click();
		await row(page, 'alice@example.com').locator('[data-job-link]').click();
		await expect(page.getByTestId('job-panel')).toBeVisible();
		await noOverflow(page);
	});

	test('tap targets reach 40 px', async ({ page }) => {
		await ready(page);
		const small = await page.evaluate(() =>
			[...document.querySelectorAll<HTMLElement>('main button, main select, main a, main label.check')]
				.filter((el) => el.offsetParent !== null)
				.map((el) => ({ text: el.getAttribute('aria-label') ?? el.textContent?.trim(), h: el.getBoundingClientRect().height }))
				.filter((el) => el.h < 40)
		);
		expect(small).toEqual([]);
	});
});

test.describe('keyboard', () => {
	test.use({ viewport: { width: 1280, height: 800 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('skip link, then select and test with the keyboard only', async ({ page }) => {
		await ready(page);
		await page.keyboard.press('Tab');
		const skip = page.getByRole('link', { name: 'Skip to content' });
		await expect(skip).toBeFocused();
		await page.keyboard.press('Enter');

		const box = row(page, 'alice@example.com').getByRole('checkbox');
		await box.focus();
		await expect(box).toBeFocused();
		await page.keyboard.press('Space');
		await expect(page.getByTestId('selection-count')).toContainText('1');

		const button = row(page, 'alice@example.com').getByRole('button', { name: 'More actions for alice@example.com' });
		await button.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('menuitem', { name: 'Test connection' })).toBeFocused();
		await page.keyboard.press('Enter');
		await expect((await openMailbox(page, 'alice@example.com')).getByTestId('test-result')).toBeVisible();

		// The row itself opens by keyboard too, and Esc gives the focus back to it.
		await page.keyboard.press('Escape');
		const link = page.locator('[data-account-link="1"]');
		await expect(link).toBeFocused();
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('job-panel')).toBeVisible();
	});

	test('a visible focus ring, menus by arrow keys, Esc closes', async ({ page }) => {
		await ready(page);
		const more = page.getByRole('button', { name: 'More actions for alice@example.com' });
		await more.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('menuitem').first()).toBeFocused();
		await page.keyboard.press('ArrowDown');
		await expect(page.getByRole('menuitem').nth(1)).toBeFocused();
		await page.keyboard.press('Escape');
		await expect(page.getByRole('menuitem')).toHaveCount(0);
		await expect(more).toBeFocused();
		const outline = await more.evaluate((el) => getComputedStyle(el).outlineStyle);
		expect(outline).toBe('solid');

		const add = page.getByRole('button', { name: 'New migration', exact: true });
		await add.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('dialog')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(page.getByRole('dialog')).toHaveCount(0);
	});
});

test.describe('reduced motion', () => {
	test.use({ contextOptions: { reducedMotion: 'reduce' }, viewport: { width: 1280, height: 800 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('nothing moves', async ({ page }) => {
		// The reduce block shrinks every CSS transition to 0.01 ms (such a transition can still be
		// listed as running for a frame) and removes CSS animations; the JS transitions in lib/ui/motion.ts
		// and the flip in QueuePanel return a zero duration. Only an effect longer than 1 ms is motion.
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		const animations = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && Number(a.effect?.getTiming().duration ?? 0) > 1).length);
		expect(animations).toBe(0);
		const duration = await page.locator('.input').first().evaluate((el) => parseFloat(getComputedStyle(el).transitionDuration));
		expect(duration).toBeLessThan(0.001);

		// The panel is there at once, without a slide.
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await row(page, 'alice@example.com').locator('[data-job-link]').click();
		expect(await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && Number(a.effect?.getTiming().duration ?? 0) > 1).length)).toBe(0);
	});
});

test.describe('languages', () => {
	test.use({ viewport: { width: 1280, height: 800 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	const WORDS: Record<string, string> = { en: 'Migrations', fr: 'Migrations', de: 'Migrationen', es: 'Migraciones', it: 'Migrazioni', pt: 'Migrações', 'pt-BR': 'Migrações', ru: 'Миграции', 'zh-Hans': '迁移' };
	for (const [locale, word] of Object.entries(WORDS)) {
		test(`${locale} loads`, async ({ page }) => {
			await page.addInitScript((value) => localStorage.setItem('PARAGLIDE_LOCALE', value), locale);
			await page.goto('/');
			await expect(page.getByRole('heading', { level: 1 })).toHaveText(word);
			await expect(page.locator('html')).toHaveAttribute('lang', locale);
		});
	}

	test('changing the language keeps the page, and the choice survives a reload', async ({ page }) => {
		await ready(page, '/settings');
		await page.getByTestId('section-appearance').getByTestId('language').selectOption('fr');
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Réglages');
		await expect(page.locator('html')).toHaveAttribute('lang', 'fr');
		await page.reload();
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Réglages');
	});
});

test.describe('settings', () => {
	test.use({ viewport: { width: 1280, height: 800 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	test('customisation is stored in the browser', async ({ page }) => {
		await ready(page, '/settings');
		await page.getByTestId('theme').selectOption('light');
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await page.getByTestId('appearance-more').locator('summary').click();
		await page.getByTestId('density').selectOption('comfortable');
		await expect(page.locator('html')).toHaveAttribute('data-density', 'comfortable');
		await page.getByRole('checkbox', { name: /Mask addresses/ }).check();
		await page.reload();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

		await page.getByRole('link', { name: 'Migrations' }).first().click();
		await expect(page.getByTestId('account-email').first()).not.toContainText('alice@example.com');
	});

	test('server test: every protocol, and the verdict stays after a reload', async ({ page }) => {
		await ready(page, '/servers');
		const row = page.locator('[data-server="Production"]');
		await expect(row.getByTestId('server-verdict')).toContainText('Not verified');
		await page.getByRole('button', { name: 'Test Production' }).click();
		const result = page.getByTestId('server-test');
		await expect(result).toContainText('IMAP');
		await expect(result).toContainText('CardDAV');
		await expect(row.getByTestId('server-verdict')).toContainText('Check failed');
		await page.reload();
		await expect(page.locator('[data-server="Production"]').getByTestId('server-verdict')).toContainText('Check failed');
	});

	test('lists the connector catalogue, and tolerates a server without the route', async ({ page }) => {
		await ready(page, '/servers');
		await expect(page.getByTestId('section-servers')).toBeVisible();
		await expect(page.getByTestId('connectors')).toContainText('Future mail service');
		await expect(page.getByTestId('connectors')).toContainText('Any IMAP mailbox.');

		await page.route('**/api/connectors', (route) => route.fulfill({ status: 404, json: { detail: 'not found' } }));
		await page.reload();
		await expect(page.getByTestId('section-servers')).toBeVisible();
		await expect(page.getByTestId('connectors')).toHaveCount(0);
	});

	test('a wrong login is refused, then signing in works', async ({ page, context }) => {
		await context.clearCookies();
		await page.goto('/');
		await page.getByTestId('login-password').fill('nope');
		await page.getByRole('button', { name: 'Sign in' }).click();
		await expect(page.getByRole('alert')).toContainText('Incorrect password');
		await page.getByTestId('login-password').fill('admin');
		await page.getByRole('button', { name: 'Sign in' }).click();
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Migrations');
	});
});
