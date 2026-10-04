import { defineConfig, devices } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';
const DEFAULT_API_BASE = 'http://127.0.0.1:8686';
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

const FIXTURE_BUILDER_MODULE = 'apps.webui.frontend.tests.e2e.support.deckload_fixture';
export const FIXTURE_DATA_DIR = join(
	FRONTEND_ROOT,
	'tests',
	'e2e',
	'fixtures',
	'performance-playwright-data'
);
export const FIXTURE_MANIFEST_PATH = join(FIXTURE_DATA_DIR, 'fixture-manifest.json');

/**
 * Fixture mode builds an isolated, analysis-backed library and manifest.
 * Real-library mode (PERFORMANCE_E2E_FIXTURE=0) copies the caller's data
 * into a disposable directory before any feedback writes. Attaching to
 * external servers leaves data isolation to the caller.
 *
 * The private preload1 demonstration and its dependent specs are unavailable
 * in this public copy. Generic /performance controls, IPC and the typed
 * preset engine remain. Fixture-generated media is not real-library release
 * evidence; a spec's own prerequisites still determine availability.
 */
/**
 * OWN BEATGRID. Since #3561 the deck refuses the legacy librosa grid for an
 * unmapped track whose own lane is `missing`, so a fixture built without the
 * own producer has NO grid on any deck and every Beat Sync spec is dark.
 * `--seed-own-beatgrid` runs the production producer (Beat This!) over the
 * generated audio, so `/anlz` serves a measured own grid. It needs the
 * verified checkpoint, named by MDT_BEATGRID_WEIGHTS (install it with
 * `python -m apps.analysis_beatgrid.weights install --from <file>`), plus
 * ffmpeg. PERFORMANCE_E2E_OWN_BEATGRID=1 requires it (the builder fails
 * loudly without the weights), =0 turns it off, and unset seeds it whenever
 * MDT_BEATGRID_WEIGHTS is set. Specs that need a grid skip with that reason
 * when it is off (support/own-beatgrid.ts).
 */
export const OWN_BEATGRID_SEEDED =
	process.env.PERFORMANCE_E2E_OWN_BEATGRID === '1' ||
	(process.env.PERFORMANCE_E2E_OWN_BEATGRID !== '0' &&
		(process.env.MDT_BEATGRID_WEIGHTS ?? '').trim() !== '');

function _buildPerformanceFixture(): void {
	execFileSync(
		'uv',
		[
			'run',
			'--no-sync',
			'python',
			'-m',
			FIXTURE_BUILDER_MODULE,
			'--data-dir',
			FIXTURE_DATA_DIR,
			'--seed-rescue-playback',
			...(OWN_BEATGRID_SEEDED ? ['--seed-own-beatgrid'] : []),
			'--manifest',
			FIXTURE_MANIFEST_PATH
		],
		{ cwd: REPOSITORY_ROOT, stdio: 'inherit' }
	);
	process.env.MDT_DATA_DIR = FIXTURE_DATA_DIR;
}

/**
 * REAL-LIBRARY MODE keeps main's isolation, unchanged (Codex P1, #1628).
 *
 * Fixture mode is isolated because the data dir was never the maintainer's. Real-library
 * mode has no such property: `PERFORMANCE_E2E_FIXTURE=0` points MDT_DATA_DIR at
 * a library he owns, and this suite dispatches 401 real `feedback_mark`
 * commands plus `performance-feedback-card-dismiss.spec.ts`, which archives a
 * synthetic pin - a permanent append to `feedback/archive-*.json`. So that mode
 * still hydrates a disposable copy first, exactly as `main` did for every mode.
 * The copy preserves the caller's rows and identifiers; controls require
 * the caller's playable, analyzed tracks.
 */
function _hydrateDisposableDataDir(): void {
	const source = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
	const disposable = join(mkdtempSync(join(tmpdir(), 'performance-e2e-data-')), 'data');
	if (existsSync(source)) {
		cpSync(source, disposable, { recursive: true });
	}
	process.env.MDT_DATA_DIR = disposable;
}
/**
 * A worker must not prepare the data dir again; it inherits the main process's.
 *
 * Playwright evaluates this config in the main process and again in every
 * worker, and a worker imports it only AFTER webServer has booted the engine
 * on MDT_DATA_DIR. Rebuilding the fixture there wrote state.db beside a live
 * engine whose own startup writes (analysis autostart) commit concurrently, and
 * `provenance.write_field` opens a deferred SAVEPOINT, so a commit between its
 * read and its write fails at once with "database is locked" instead of waiting
 * out busy_timeout (e2e gate job 109106840542, agentbox-12). Real-library mode
 * had the same shape: each worker hydrated its OWN temp copy, so specs read a
 * different MDT_DATA_DIR than the engine served. The main process exports
 * MDT_DATA_DIR before spawning workers, and Playwright passes it on.
 */
function _requireInheritedDataDir(fixtureMode: boolean): void {
	const inherited = process.env.MDT_DATA_DIR;
	if (inherited === undefined || inherited === '') {
		throw new Error(
			'performance e2e worker: MDT_DATA_DIR not inherited from the main process, which prepares the data dir before starting the engine'
		);
	}
	if (fixtureMode && inherited !== FIXTURE_DATA_DIR) {
		throw new Error(
			`performance e2e worker: fixture mode expects MDT_DATA_DIR=${FIXTURE_DATA_DIR}, got ${inherited}`
		);
	}
}

const fixtureEnabled = process.env.PERFORMANCE_E2E_FIXTURE !== '0';
const inPlaywrightWorker = process.env.TEST_WORKER_INDEX !== undefined;
if (process.env.PERFORMANCE_E2E_START_SERVERS !== '0') {
	if (inPlaywrightWorker) _requireInheritedDataDir(fixtureEnabled);
	else if (fixtureEnabled) _buildPerformanceFixture();
	else _hydrateDisposableDataDir();
}

function _loopbackUrl(name: string, raw: string): URL {
	const url = new URL(raw);
	if (url.protocol !== 'http:') {
		throw new Error(`${name} must use http, got ${url.protocol}`);
	}
	if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
		throw new Error(`${name} must be loopback, got ${url.hostname}`);
	}
	if (url.port === '') {
		throw new Error(`${name} must include an explicit port`);
	}
	if (url.pathname !== '/' || url.search !== '' || url.hash !== '') {
		throw new Error(`${name} must be an origin without path, query, or fragment`);
	}
	return url;
}

const frontend = _loopbackUrl(
	'PERFORMANCE_E2E_BASE_URL',
	process.env.PERFORMANCE_E2E_BASE_URL ?? DEFAULT_FRONTEND_BASE
);
const api = _loopbackUrl(
	'PERFORMANCE_E2E_API_BASE',
	process.env.PERFORMANCE_E2E_API_BASE ?? DEFAULT_API_BASE
);
const startServers = process.env.PERFORMANCE_E2E_START_SERVERS !== '0';

// ENGINE_CMD seam: backend boot command as a template ({host} {port}
// {data_dir}); unset means the legacy webui server, verbatim as before.
// This config carries no data dir of its own, so {data_dir} demands
// MDT_DATA_DIR explicitly rather than guessing.
const ENGINE_CMD_TEMPLATE =
	process.env.ENGINE_CMD ??
	'uv run --no-sync python -m apps.webui.server --host {host} --port {port} --prod';
if (ENGINE_CMD_TEMPLATE.includes('{data_dir}') && !process.env.MDT_DATA_DIR) {
	throw new Error('ENGINE_CMD references {data_dir} but MDT_DATA_DIR is not set');
}
const engineCmd = ENGINE_CMD_TEMPLATE.replaceAll('{host}', api.hostname)
	.replaceAll('{port}', String(api.port))
	.replaceAll('{data_dir}', process.env.MDT_DATA_DIR ?? '');
const autoplayOverrideEnabled = process.env.PERFORMANCE_E2E_AUTOPLAY !== '0';
const audioActivationPolicy =
	process.env.PERFORMANCE_E2E_AUDIO_ACTIVATION ??
	(autoplayOverrideEnabled ? 'auto' : 'require-gesture');
if (audioActivationPolicy !== 'auto' && audioActivationPolicy !== 'require-gesture') {
	throw new Error(
		`PERFORMANCE_E2E_AUDIO_ACTIVATION must be auto or require-gesture, got ${audioActivationPolicy}`
	);
}

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'performance-*.spec.ts',
	testIgnore:
		process.env.PERFORMANCE_E2E_CUE_QA === '1' ? [] : 'performance-cue-marker-qa.spec.ts',
	fullyParallel: false,
	// One worker only: fixed loopback ports, singleton engine + AudioContext per page,
	// and real-library mode hydrates a disposable data dir shared across specs.
	workers: 1,
	reporter: [['list']],
	retries: 0,
	timeout: 120_000,
	expect: { timeout: 15_000 },
	...(startServers
		? {
				webServer: [
					{
						command: guardedWebServerCommand('performance-engine', engineCmd),
						cwd: REPOSITORY_ROOT,
						url: `${api.origin}/api/v1/health`,
						reuseExistingServer: false,
						timeout: 120_000
					},
					{
						command: guardedWebServerCommand('performance-vite', 'pnpm exec vite --config tests/e2e/vite.performance.config.ts'),
						cwd: FRONTEND_ROOT,
						url: `${frontend.origin}/performance`,
						reuseExistingServer: false,
						timeout: 120_000,
						env: {
							...process.env,
							PERFORMANCE_E2E_BASE_URL: frontend.origin,
							PERFORMANCE_E2E_API_BASE: api.origin,
							VITE_PERFORMANCE_AUDIO_ACTIVATION: audioActivationPolicy
						}
					}
				]
			}
		: {}),
	use: {
		baseURL: frontend.origin,
		viewport: { width: 2000, height: 1250 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		launchOptions: {
			args: autoplayOverrideEnabled
				? ['--autoplay-policy=no-user-gesture-required']
				: [
						'--autoplay-policy=user-gesture-required',
						'--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies'
					]
		}
	},
	projects: [
		{
			name: 'performance-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
