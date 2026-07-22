import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests/e2e',
	// Real-library performance contracts have their own servers and config.
	// Run them explicitly with `pnpm test:e2e:performance`.
	testIgnore: '**/performance-*.spec.ts',
	timeout: 30_000,
	webServer: {
		command: 'pnpm dev',
		port: 5173,
		reuseExistingServer: !process.env.CI,
	},
	use: { baseURL: 'http://127.0.0.1:5173' },
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
