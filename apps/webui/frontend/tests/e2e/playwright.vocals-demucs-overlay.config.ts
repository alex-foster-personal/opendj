/**
 * Vocals demucs overlay acceptance (#274): hydrated listing row paints PreviewStrip blue bars.
 *
 * Opt-in only. Run::
 *
 *     MDT_LIVE_DEMUCS_ACCEPTANCE=1 pnpm test:e2e:vocals-demucs-overlay
 */
import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveEndpoints } from './vocals-demucs-overlay-endpoints';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

if (process.env.MDT_LIVE_DEMUCS_ACCEPTANCE !== '1') {
	throw new Error(
		'vocals demucs overlay e2e is opt-in: set MDT_LIVE_DEMUCS_ACCEPTANCE=1'
	);
}

const endpoints = resolveEndpoints();

export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'vocals-demucs-data'
);
export const FIXTURE_MANIFEST = join(FIXTURE_DATA_DIR, 'fixture-manifest.json');

process.env.VOCALS_DEMUCS_OVERLAY_API_BASE = endpoints.backendOrigin;
process.env.VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT = String(endpoints.frontendPort);

const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`vocals demucs overlay needs the production build: ${BUILD_INDEX} missing. ` +
			'Run `pnpm build` in apps/webui/frontend first.'
	);
}

const FIXTURE_BUILDER = join(
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'support',
	'vocals_demucs_fixture.py'
);

const engineEnv = {
	...process.env,
	MDT_DATA_DIR: FIXTURE_DATA_DIR,
	MDT_LIBRARY_MODE: 'local',
	MUSIC_DJ_FRONTEND_PORT: String(endpoints.frontendPort),
	MUSIC_DJ_BACKEND_PORT: String(endpoints.backendPort),
	WEB_CONCURRENCY: '',
	HOME: SANDBOX_HOME,
	MDT_LIVE_DEMUCS_ACCEPTANCE: '1'
};

export default defineConfig({
	testDir: '.',
	testMatch: ['vocals-demucs-overlay.spec.ts'],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 600_000,
	globalTimeout: 1_200_000,
	expect: { timeout: 30_000 },
	reporter: [['list']],
	webServer: [
		{
			command: [
				`uv run --no-sync python ${FIXTURE_BUILDER}`,
				`--data-dir ${FIXTURE_DATA_DIR}`,
				`--manifest ${FIXTURE_MANIFEST}`,
				'&&',
				'uv run --no-sync python -m apps.webui.server',
				`--host 127.0.0.1 --port ${endpoints.backendPort} --prod`,
				`--data-dir ${FIXTURE_DATA_DIR}`
			].join(' '),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 600_000,
			env: engineEnv
		},
		{
			command: 'pnpm exec vite --config tests/e2e/vite.vocals-demucs-overlay.config.ts',
			cwd: FRONTEND_ROOT,
			url: `${endpoints.frontendOrigin}/performance`,
			reuseExistingServer: false,
			timeout: 120_000,
			env: {
				...process.env,
				VOCALS_DEMUCS_OVERLAY_API_BASE: endpoints.backendOrigin,
				VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT: String(endpoints.frontendPort)
			}
		}
	],
	use: {
		baseURL: endpoints.frontendOrigin,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'vocals-demucs-overlay-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } }
		}
	]
});
