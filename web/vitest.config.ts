import { defineConfig } from 'vitest/config';

// Pure logic only: no Svelte plugin, no DOM. The UI itself is covered by Playwright (tests/ui).
export default defineConfig({
	test: { include: ['tests/unit/**/*.test.ts'], environment: 'node' }
});
