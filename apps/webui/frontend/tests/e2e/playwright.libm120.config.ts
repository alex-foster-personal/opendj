import { defineConfig, devices } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	LIBM120_DATA_DIR as DATA_DIR,
	LIBM120_PLATFORM_SUPPORTED,
	LIBM120_PORT as PORT,
	LIBM120_PROJECT,
	LIBM120_SEED,
	LIBM120_TRACKS as TRACKS
} from './libm120-settings';
import { guardedWebServerCommand, VENV_PYTHON } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

mkdirSync(DATA_DIR, { recursive: true });
const buildIndex = join(FRONTEND_ROOT, 'build', 'index.html');
if (LIBM120_PLATFORM_SUPPORTED && !existsSync(buildIndex)) {
	throw new Error(`LIBM-120 production build missing: ${buildIndex}. Run pnpm build first.`);
}

// Same quoting as the root config: every argument is single-quoted, so a
// fixture directory with spaces or shell metacharacters reaches the generator
// and the engine as one --data-dir argument. Both run under the resolved repo
// venv interpreter the guard already requires, so no bare `uv` has to be on PATH.
function shellArgument(value: string): string {
	return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

const generateCommand = [
	VENV_PYTHON,
	'-m',
	'scripts.perf.generate_library_scale_fixture',
	'--data-dir',
	DATA_DIR,
	'--tracks',
	String(TRACKS),
	'--seed',
	String(LIBM120_SEED)
]
	.map(shellArgument)
	.join(' ');
const engineCommand = [
	VENV_PYTHON,
	'-m',
	'apps.engine_core',
	'serve',
	'--data-dir',
	DATA_DIR,
	'--host',
	'127.0.0.1',
	'--port',
	String(PORT)
]
	.map(shellArgument)
	.join(' ');
const fixtureCommand = `${generateCommand} && ${engineCommand}`;

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'libm120-scale-bench.spec.ts',
	workers: 1,
	fullyParallel: false,
	retries: 0,
	timeout: 120_000,
	// 20k playlist + All Tracks fills are sequential; slow hosts need headroom.
	globalTimeout: 3_600_000,
	reporter: [['list']],
	// No webServer where the shared launcher cannot run; the spec then skips
	// with LIBM120_POSIX_ONLY_REASON instead of failing on a POSIX path.
	...(LIBM120_PLATFORM_SUPPORTED
		? {
				webServer: {
					command: guardedWebServerCommand('libm120-production-engine', fixtureCommand),
					cwd: REPOSITORY_ROOT,
					url: `http://127.0.0.1:${PORT}/api/v1/health`,
					reuseExistingServer: false,
					timeout: 180_000,
					env: {
						...process.env,
						HOME: join(DATA_DIR, 'sandbox-home'),
						MDT_DATA_DIR: DATA_DIR,
						MDT_LIBRARY_MODE: 'local',
						WEB_CONCURRENCY: ''
					}
				}
			}
		: {}),
	use: {
		...devices['Desktop Chrome'],
		baseURL: `http://127.0.0.1:${PORT}`,
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] }
	},
	projects: [{ name: LIBM120_PROJECT, use: { ...devices['Desktop Chrome'] } }]
});
