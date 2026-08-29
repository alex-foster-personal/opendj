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
		// --host is not decoration. Vite's default binds `localhost`, which on
		// this machine resolves to ::1 ONLY, while baseURL below dials
		// 127.0.0.1 -- every test in every suite then died on
		// ERR_CONNECTION_REFUSED while `port` reported the server up. Bind the
		// exact address the tests connect to, so the two can never disagree.
		command: `pnpm dev --host 127.0.0.1`,
		port: ports.frontendPort,
		reuseExistingServer: false,
	},
	use: { baseURL: `http://127.0.0.1:${ports.frontendPort}` },
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
