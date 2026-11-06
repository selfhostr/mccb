import { defineConfig, devices } from '@playwright/test';

// Tests live in tests/ui and drive the production build (npm run build first) against the mock API.
// One worker: the mock holds one shared state, reset before each test.
export default defineConfig({
	testDir: '../tests/ui',
	testMatch: '*.spec.ts',
	// The static demo has its own config (playwright.demo.config.ts): another server, another build.
	testIgnore: 'demo.spec.ts',
	outputDir: 'test-results',
	workers: 1,
	fullyParallel: false,
	timeout: 30_000,
	expect: { timeout: 5_000 },
	reporter: process.env.CI ? [['github'], ['list']] : 'list',
	use: { baseURL: 'http://127.0.0.1:4173', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
	webServer: {
		command: 'node ../tests/ui/mock-api.mjs 4173',
		url: 'http://127.0.0.1:4173/api/health',
		reuseExistingServer: !process.env.CI
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
