import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

export default defineConfig({
	testDir: '.',
	testMatch: 'play-analytics.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	expect: { timeout: 10_000 },
	webServer: [
		{
			command:
				guardedWebServerCommand('play-analytics-engine', 'uv run --no-sync --python 3.11 python -m tests.play_analytics.e2e_server --db .tmp/play-analytics-e2e.db --port 9414'),
			cwd: REPOSITORY_ROOT,
			url: 'http://127.0.0.1:9414/health',
			reuseExistingServer: false,
			timeout: 60_000
		},
		{
			command: guardedWebServerCommand('play-analytics-vite', 'pnpm exec vite --config tests/e2e/vite.play-analytics.config.ts'),
			cwd: FRONTEND_ROOT,
			url: 'http://127.0.0.1:5214/play-analytics',
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: {
		baseURL: 'http://127.0.0.1:5214',
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'analytics-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
