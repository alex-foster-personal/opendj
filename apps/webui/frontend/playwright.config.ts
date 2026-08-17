import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { claimAndCheckWebuiDevConfigOnce } from './webui-port-config';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
// Once = claim/check in the main process only; workers reuse its payload
// (they re-import this config after vite has bound the claimed port).
const ports = claimAndCheckWebuiDevConfigOnce(REPOSITORY_ROOT, 'frontend');

export default defineConfig({
	testDir: './tests/e2e',
	// Real-library performance contracts have their own servers and config.
	// Run them explicitly with `pnpm test:e2e:performance`.
	testIgnore: '**/performance-*.spec.ts',
	timeout: 30_000,
	webServer: {
		command: 'pnpm dev',
		port: ports.frontendPort,
		reuseExistingServer: false,
	},
	use: { baseURL: `http://127.0.0.1:${ports.frontendPort}` },
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
