/**
 * Library search error visibility at 200% browser zoom (issue #12949).
 *
 * Run with:
 *   pnpm exec playwright test --config tests/e2e/playwright.library-error-zoom.config.ts
 */
import { defineConfig, devices } from '@playwright/test';
import { mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveLibraryErrorZoomEndpoints } from './library-error-zoom-endpoints';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const endpoints = resolveLibraryErrorZoomEndpoints();

const FIXTURE_DATA_DIR = join(
	REPOSITORY_ROOT,
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'fixtures',
	'library-error-zoom-data'
);
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');

if (process.env.TEST_WORKER_INDEX === undefined) {
	rmSync(FIXTURE_DATA_DIR, { recursive: true, force: true });
}
mkdirSync(SANDBOX_HOME, { recursive: true });

process.env.LIBRARY_ERROR_ZOOM_API_BASE = endpoints.backendOrigin;
process.env.LIBRARY_ERROR_ZOOM_BACKEND_PORT = String(endpoints.backendPort);
process.env.LIBRARY_ERROR_ZOOM_FRONTEND_PORT = String(endpoints.frontendPort);

function shellArgument(value: string): string {
	return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

const FIXTURE_COMMAND = [
	'uv',
	'run',
	'--no-sync',
	'python',
	'-m',
	'apps.webui.frontend.tests.e2e.support.bpm_provenance_fixture',
	'--data-dir',
	FIXTURE_DATA_DIR
]
	.map(shellArgument)
	.join(' ');
const ENGINE_COMMAND = [
	'uv',
	'run',
	'--no-sync',
	'python',
	'-m',
	'apps.engine_core',
	'serve',
	'--data-dir',
	FIXTURE_DATA_DIR,
	'--host',
	'127.0.0.1',
	'--port',
	String(endpoints.backendPort)
]
	.map(shellArgument)
	.join(' ');
const SERVER_COMMAND = `${FIXTURE_COMMAND} && ${ENGINE_COMMAND}`;

const engineEnv = {
	...process.env,
	MDT_DATA_DIR: FIXTURE_DATA_DIR,
	MDT_LIBRARY_MODE: 'local',
	MUSIC_DJ_FRONTEND_PORT: String(endpoints.frontendPort),
	MUSIC_DJ_BACKEND_PORT: String(endpoints.backendPort),
	WEB_CONCURRENCY: '',
	HOME: SANDBOX_HOME,
	SPOTIFY_CLIENT_ID: '',
	OPENDJ_SPOTIFY_EPHEMERAL_REDIRECT: '1'
};

const viteConfig = fileURLToPath(new URL('vite.library-error-zoom.config.ts', import.meta.url));

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'library-error-zoom.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	expect: { timeout: 15_000 },
	reporter: [['list']],
	webServer: [
		{
			command: guardedWebServerCommand('library-error-zoom-engine', SERVER_COMMAND),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: engineEnv
		},
		{
			command: guardedWebServerCommand(
				'library-error-zoom-vite',
				`pnpm exec vite --config ${viteConfig}`
			),
			cwd: FRONTEND_ROOT,
			url: `${endpoints.frontendOrigin}/`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: {
				...process.env,
				LIBRARY_ERROR_ZOOM_API_BASE: endpoints.backendOrigin,
				LIBRARY_ERROR_ZOOM_BACKEND_PORT: String(endpoints.backendPort),
				LIBRARY_ERROR_ZOOM_FRONTEND_PORT: String(endpoints.frontendPort)
			}
		}
	],
	use: {
		baseURL: endpoints.frontendOrigin,
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'library-error-zoom-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
