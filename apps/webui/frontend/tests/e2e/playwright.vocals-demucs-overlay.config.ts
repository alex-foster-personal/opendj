/**
 * Vocals demucs overlay acceptance (#274): hydrated listing row paints PreviewStrip blue bars.
 *
 * Opt-in only. Run::
 *
 *     MDT_LIVE_DEMUCS_ACCEPTANCE=1 \
 *     VOCALS_DEMUCS_OVERLAY_SOURCE_AUDIO=/abs/track-with-vocals.mp3 \
 *     VOCALS_DEMUCS_OVERLAY_CLIP_START_S=55 \
 *     pnpm test:e2e:vocals-demucs-overlay
 */
import { defineConfig, devices } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { guardedWebServerCommand } from './support/guarded-web-server';
import { resolveEndpoints } from './vocals-demucs-overlay-endpoints';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

if (process.env.MDT_LIVE_DEMUCS_ACCEPTANCE !== '1') {
	throw new Error(
		'vocals demucs overlay e2e is opt-in: set MDT_LIVE_DEMUCS_ACCEPTANCE=1'
	);
}

const endpoints = resolveEndpoints();

// Real vocal audio is required: htdemucs finds zero vocal regions in the
// synthetic tone the other e2e fixtures share, so the overlay could not paint.
const SOURCE_AUDIO = process.env.VOCALS_DEMUCS_OVERLAY_SOURCE_AUDIO;
const CLIP_START_S = process.env.VOCALS_DEMUCS_OVERLAY_CLIP_START_S;
if (!SOURCE_AUDIO || !CLIP_START_S) {
	throw new Error(
		'vocals demucs overlay e2e needs VOCALS_DEMUCS_OVERLAY_SOURCE_AUDIO (absolute path ' +
			'to a track with vocals) and VOCALS_DEMUCS_OVERLAY_CLIP_START_S (seconds)'
	);
}

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

// The engine boots under SANDBOX_HOME, but the fixture builder's demucs run
// must reuse the CALLER's uv and torch caches. Under the sandbox HOME, uv
// re-resolved the worker env (843 MB of torch) and torch re-downloaded the
// htdemucs checkpoint (80 MB), both inside the repo tree where the quality
// gate scans them. Resolve the real locations once, from the caller's env.
const CALLER_UV_CACHE_DIR = execFileSync('uv', ['cache', 'dir'], { encoding: 'utf8' }).trim();
const CALLER_UV_PYTHON_DIR = execFileSync('uv', ['python', 'dir'], { encoding: 'utf8' }).trim();
const CALLER_TORCH_HOME = process.env.TORCH_HOME ?? join(homedir(), '.cache', 'torch');
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
	UV_CACHE_DIR: CALLER_UV_CACHE_DIR,
	UV_PYTHON_INSTALL_DIR: CALLER_UV_PYTHON_DIR,
	TORCH_HOME: CALLER_TORCH_HOME,
	MDT_LIVE_DEMUCS_ACCEPTANCE: '1'
};

const engineCmd = [
	`uv run --no-sync python ${FIXTURE_BUILDER}`,
	`--data-dir ${FIXTURE_DATA_DIR}`,
	`--manifest ${FIXTURE_MANIFEST}`,
	`--source-audio ${JSON.stringify(SOURCE_AUDIO)}`,
	`--clip-start-s ${CLIP_START_S}`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	`--host 127.0.0.1 --port ${endpoints.backendPort}`
].join(' ');

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
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
			command: guardedWebServerCommand('vocals-demucs-overlay-engine', engineCmd),
			cwd: REPOSITORY_ROOT,
			url: `${endpoints.backendOrigin}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 600_000,
			env: engineEnv
		},
		{
			command: guardedWebServerCommand(
				'vocals-demucs-overlay-vite',
				'pnpm exec vite --config tests/e2e/vite.vocals-demucs-overlay.config.ts'
			),
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
