import { defineConfig, devices } from '@playwright/test';

// The static demo: npm run build:demo first. It is served by tests/ui/serve-demo.mjs with the headers of
// demo-headers and the 404.html fallback of the real host, and no API behind it.
export default defineConfig({
	testDir: '../tests/ui',
	testMatch: 'demo.spec.ts',
	outputDir: 'test-results',
	workers: 1,
	fullyParallel: false,
	timeout: 30_000,
	expect: { timeout: 5_000 },
	reporter: process.env.CI ? [['github'], ['list']] : 'list',
	use: { baseURL: 'http://127.0.0.1:4174', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	webServer: {
		command: 'node ../tests/ui/serve-demo.mjs 4174',
		url: 'http://127.0.0.1:4174/demo/',
		reuseExistingServer: !process.env.CI
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
