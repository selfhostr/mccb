import { expect, noOverflow, ready, row, test } from './helpers';

test.describe('at 390 px', () => {
	test.use({ viewport: { width: 390, height: 844 } });
	test.beforeEach(async ({ signedIn }) => signedIn);

	for (const url of ['/', '/files', '/settings']) {
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

		const button = row(page, 'alice@example.com').getByRole('button', { name: /^Test connection/ });
		await button.focus();
		await page.keyboard.press('Enter');
		await expect(row(page, 'alice@example.com').getByTestId('test-result')).toBeVisible();
	});

	test('a visible focus ring, menus by arrow keys, Esc closes', async ({ page }) => {
		await ready(page);
		const add = page.getByRole('button', { name: 'Add', exact: true });
		await add.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('menuitem').first()).toBeFocused();
		await page.keyboard.press('ArrowDown');
		await expect(page.getByRole('menuitem').nth(1)).toBeFocused();
		await page.keyboard.press('Escape');
		await expect(page.getByRole('menuitem')).toHaveCount(0);
		await expect(add).toBeFocused();
		const outline = await add.evaluate((el) => getComputedStyle(el).outlineStyle);
		expect(outline).toBe('solid');

		await page.keyboard.press('Enter');
		await expect(page.getByRole('menuitem').first()).toBeFocused();
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
		await ready(page);
		await row(page, 'alice@example.com').getByRole('checkbox').check();
		const animations = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length);
		expect(animations).toBe(0);
		const duration = await page.locator('.input').first().evaluate((el) => parseFloat(getComputedStyle(el).transitionDuration));
		expect(duration).toBeLessThan(0.001);

		// The panel is there at once, without a slide.
		await page.getByTestId('selection-bar').getByRole('button', { name: 'Import' }).click();
		await page.getByTestId('import-confirm').click();
		await row(page, 'alice@example.com').locator('[data-job-link]').click();
		expect(await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length)).toBe(0);
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
		await page.getByTestId('density').selectOption('comfortable');
		await expect(page.locator('html')).toHaveAttribute('data-density', 'comfortable');
		await page.getByRole('checkbox', { name: /Mask addresses/ }).check();
		await page.reload();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

		await page.getByRole('link', { name: 'Migrations' }).first().click();
		await expect(page.getByTestId('account-email').first()).not.toContainText('alice@example.com');
	});

	test('server test: every protocol, and it says it is not stored', async ({ page }) => {
		await ready(page, '/settings');
		await page.getByRole('button', { name: 'Test Production' }).click();
		const result = page.getByTestId('server-test');
		await expect(result).toContainText('IMAP');
		await expect(result).toContainText('CardDAV');
		await expect(page.getByTestId('section-servers')).toContainText('does not store');
	});

	test('tolerates absent routes and renders the connectors it gets', async ({ page }) => {
		await ready(page, '/settings');
		await expect(page.getByTestId('connectors')).toHaveCount(0);
		await expect(page.getByTestId('section-notifications')).toBeVisible();

		await page.route('**/api/connectors', (route) => route.fulfill({ json: [{ id: 'imap', name: 'Generic IMAP', description: 'Any IMAP server' }] }));
		await page.reload();
		await expect(page.getByTestId('connectors')).toContainText('Generic IMAP');
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
