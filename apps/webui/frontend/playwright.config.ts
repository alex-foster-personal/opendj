import { defineConfig, devices } from '@playwright/test';
import { mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { claimAndCheckWebuiDevConfigOnce } from './webui-port-config';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const FIXTURE_DATA_DIR = join(
	REPOSITORY_ROOT,
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'fixtures',
	'root-playwright-data'
);
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
const FIXTURE_PARENT_DIR = join(
	REPOSITORY_ROOT,
	'apps',
	'webui',
	'frontend',
	'tests',
	'e2e',
	'fixtures'
);
if (dirname(FIXTURE_DATA_DIR) !== FIXTURE_PARENT_DIR) {
	throw new Error(`Refusing to reset fixture outside ${FIXTURE_PARENT_DIR}: ${FIXTURE_DATA_DIR}`);
}
if (process.env.TEST_WORKER_INDEX === undefined) {
	rmSync(FIXTURE_DATA_DIR, { recursive: true, force: true });
}
mkdirSync(SANDBOX_HOME, { recursive: true });

// Once = claim/check in the main process only; workers reuse its payload
// (they re-import this config after vite has bound the claimed port).
const ports = claimAndCheckWebuiDevConfigOnce(REPOSITORY_ROOT, 'frontend');

// The root suite contains browser tests that use the Vite /api proxy. Build a
// real, throwaway library before the engine starts so `pnpm test:e2e` has no
// undeclared daemon or personal-library prerequisite.
function shellArgument(value: string): string {
	return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

const FIXTURE_COMMAND = [
	'uv', 'run', '--no-sync', 'python', '-m',
	'apps.webui.frontend.tests.e2e.support.deckload_fixture',
	'--data-dir', FIXTURE_DATA_DIR, '--seed-playlists'
].map(shellArgument).join(' ');
const ENGINE_COMMAND = [
	'uv', 'run', '--no-sync', 'python', '-m', 'apps.engine_core', 'serve',
	'--data-dir', FIXTURE_DATA_DIR, '--host', '127.0.0.1', '--port', String(ports.backendPort)
].map(shellArgument).join(' ');
const SERVER_COMMAND = `${FIXTURE_COMMAND} && ${ENGINE_COMMAND}`;

export default defineConfig({
	testDir: './tests/e2e',
	// EVERY spec that owns a dedicated config is ignored here, because this
	// config starts vite and nothing else. Before this list existed the only
	// exclusion was performance-*, so the other eight were ALSO claimed by
	// this harness, which has no backend, no production build and the wrong
	// baseURL for them. That was not merely redundant: `savepoint-smoke.spec.ts`
	// throws at MODULE SCOPE when SAVEPOINT_SMOKE_API_BASE is unset (only its
	// own config sets it), and one module-scope throw fails Playwright's whole
	// collection. `pnpm test:e2e` therefore reported "Total: 0 tests in 0 files"
	// on main and could not run a single test. That is a large part of why the
	// e2e clause of the merge gate was never wired into CI (issue #624).
	//
	// Run each of these through its own config, or via its `just` recipe.
	// setup-entry-points.spec.ts is deliberately NOT here: its double run under
	// vite/chromium AND the webkit artifact config is documented in both files.
	testIgnore: [
		// 30s test-level timeout here makes #1385's 45s toBeVisible allowance
		// inert (Playwright counts test-function time toward the timeout), and
		// the unconditional 5s boot-burst wait after it can then blow the
		// budget too -- Sol P2 BLOCKING on #1463, thread 3961468764. Runs only
		// under playwright.rekordbox-gate.config.ts (60s timeout) instead.
		'**/artwork-reader-unavailable.spec.ts', // playwright.rekordbox-gate.config.ts (30s root timeout too tight, #1463 thread 3961468764)
		'**/missing-tracks-folder.spec.ts', // playwright.rekordbox-gate.config.ts
		'**/boot-burst.spec.ts', // playwright.boot-burst.config.ts (real library benchmark)
		'**/library-playlist-switch-latency.spec.ts', // playwright.playlist-switch-latency.config.ts (PERF-UI-05 gate)
		'**/stem-decode-bench.spec.ts', // playwright.stem-decode-bench.config.ts (production-build bench)
		'**/comment-hotkey-browser.spec.ts', // playwright.comment-hotkey-gate.config.ts (real backend)
		'**/autoplay-stall-browser.spec.ts', // playwright.autoplay-stall-gate.config.ts (real backend)
		'**/performance-*.spec.ts', // playwright.performance.config.ts (real library)
		'**/kpi-boot-library-capture.spec.ts', // playwright.kpi-boot-library-capture.config.ts (PERF-UI-03 boot KPI capture; operator-only)
		'**/library-mode-perf-capture.spec.ts', // playwright.library-mode-perf.config.ts (PERFMODE-14 library-mode capture; operator-only)
		'**/kpi-login-capture.spec.ts', // playwright.kpi-capture.config.ts (S13 login KPI capture; operator-only)
		'**/kpi-s2-capture.spec.ts', // playwright.kpi-s2-capture.config.ts (S2 press-to-audible KPI capture; operator-only)
		'**/library-mode-perf-capture.spec.ts', // playwright.library-mode-perf.config.ts (library-mode KPI capture; operator-only)
		'**/meter-artifact.spec.ts', // playwright.meter-artifact.config.ts (built artifact)
		'**/preflight-gate.spec.ts', // playwright.preflight-gate.config.ts (two real backends)
		// Same owner as the line above, and for the same reason: it imports
		// that config's PREFLIGHT_GATE_BROKEN_ORIGIN and drives the second
		// (empty-library) backend on port 5323, which this config does not
		// start. Collected here it could only ever report ERR_CONNECTION
		// REFUSED. Added with the spec in 1f97fe0dd (#2722), missed then.
		'**/fresh-install-onboarding.spec.ts', // playwright.preflight-gate.config.ts (empty-library backend)
		'**/cloudsync-ui.spec.ts', // playwright.cloudsync-ui.config.ts (real hub + spoke engines)
		'**/savepoint-smoke.spec.ts', // playwright.savepoint.config.ts (real library)
		'**/webkit-deckload.spec.ts', // playwright.webkit-deckload.config.ts (built artifact)
		'**/deckload-smoke.spec.ts', // playwright.webkit-deckload.config.ts (built artifact, chromium+webkit, #770)
		'**/playlist-detail.spec.ts', // playwright.webkit-deckload.config.ts (built artifact, fixture playlists, #859)
		'**/desktop-setup.spec.ts', // playwright.desktop-setup.config.ts (static http.server)
		'**/play-analytics.spec.ts', // playwright.play-analytics.config.ts (fixture server)
		'**/library-wheel.spec.ts', // playwright.library-wheel.config.ts (real engine, genre fixture)
		'**/rekordbox-writeback-disabled.spec.ts', // playwright.rekordbox-gate.config.ts
		'**/hot-cue-mapping-gate.spec.ts', // playwright.hotcue-mapping-gate.config.ts (real backend, fixture library)
		'**/lyrics-words.spec.ts', // playwright.lyrics-words.config.ts (real backend, words fixture)
		'**/stems-progress.spec.ts', // playwright.stems.config.ts (engine + ffmpeg)
		'**/midi-maps-resync.spec.ts', // playwright.midi-maps-resync.config.ts (engine + real WS)
		'**/library-jobs-ordering.spec.ts', // playwright.library-jobs.config.ts (dry runner)
		'**/stretch-artifact.spec.ts', // playwright.stretch-artifact.config.ts (built artifact)
		'**/stretch-quality.spec.ts', // playwright.stretch-quality.config.ts (no server)
		'**/full-reload-gate.spec.ts', // playwright.full-reload-gate.config.ts (own vite instance, r3920753724)
		'**/audio-soak.spec.ts', // playwright.audio-soak.config.ts (built artifact, MINUTES; `just test-audio-soak`)
		'**/autoplay-error-hunt.spec.ts' // playwright.autoplay-error-hunt.config.ts (MINUTES; `just test-autoplay-hunt`)
	],
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	webServer: [
		{
			command: SERVER_COMMAND,
			cwd: REPOSITORY_ROOT,
			url: `http://127.0.0.1:${ports.backendPort}/api/v1/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: {
				...process.env,
				MDT_DATA_DIR: FIXTURE_DATA_DIR,
				MDT_LIBRARY_MODE: 'local',
				WEB_CONCURRENCY: '',
				HOME: SANDBOX_HOME
			}
		},
		{
			// --host is not decoration. Vite's default binds `localhost`, which on
			// this machine resolves to ::1 ONLY, while baseURL below dials
			// 127.0.0.1 -- every test in every suite then died on
			// ERR_CONNECTION_REFUSED while `port` reported the server up. Bind the
			// exact address the tests connect to, so the two can never disagree.
			command: `pnpm dev --host 127.0.0.1`,
			cwd: fileURLToPath(new URL('./', import.meta.url)),
			url: `http://127.0.0.1:${ports.frontendPort}`,
			reuseExistingServer: false,
			timeout: 90_000
		}
	],
	use: { baseURL: `http://127.0.0.1:${ports.frontendPort}` },
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
