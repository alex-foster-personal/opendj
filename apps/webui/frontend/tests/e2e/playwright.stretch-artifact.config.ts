/**
 * Signalsmith worklet gate, run against the BUILT artifact rather than a dev
 * server.
 *
 * The package registers its AudioWorklet by `Function.toString()`-ing its own
 * bundled code into a Blob. Production bundling breaks that string (esbuild
 * lowers the processor class field into a chunk-scope helper the blob never
 * contains, so the worklet constructor throws a silent ReferenceError and
 * creation times out after 15s) and WKWebView refuses `blob:` URLs for
 * `audioWorklet.addModule` outright. Vite SERVES the package untransformed in
 * dev, so a dev-server suite passes on code that cannot load a single track in
 * the installed app. Only the built artifact can catch this class, which is why
 * this config builds and then serves `build/` over plain http instead of
 * booting Vite.
 *
 * Run with::
 *
 *     pnpm exec playwright test --config tests/e2e/playwright.stretch-artifact.config.ts
 *
 * Requirements (mini-PRD):
 *
 * - ✔︎ ✅ 🎯 Serve the real build output, never a dev server.
 *     [if] the config boots Vite [then ⛔️] the transform under test is skipped.
 * - ✔︎ ✅ 🎯 Own a fixed port outside every reserved pair.
 *     [if] the port is one another lane owns [then ⛔️] the module loads.
 * - ✔︎ ✅ 🎯 Prove the asset in both engines.
 *     [if] only chromium runs [then ⛔️] the WKWebView `blob:` refusal ships.
 *
 * Acceptance tests:
 *
 * - [if] the port collides with a live lane service [then ⛔️] the run starts.
 * - [if] a server is already listening on this port [then ⛔️] the suite
 *   attaches to it, because it would then measure somebody else's build.
 * - [if] the worklet handshake flakes [then ⛔️] a retry hides it.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/** This suite's own port, checked free at authoring time. */
export const STRETCH_ARTIFACT_PORT = 5311;

/**
 * Ports the primary checkout, the live lane services and every other per-suite
 * e2e config already own. Binding one of these would either fight a running
 * server or silently measure it instead of this suite's build.
 */
const RESERVED = new Set([
	8585, 5173, // primary checkout backend/frontend
	8685, 9405, // live lane services
	8682, 9402, // rebuild bake-off lane
	5216, 9473, // desktop-setup suite (static page, deliberately dead engine)
	8688, 9408, // stems e2e default pair
	5273, 8686, // reserved for neighbouring lanes
	5399 // rekordbox-gate suite
]);

if (RESERVED.has(STRETCH_ARTIFACT_PORT)) {
	throw new Error(
		`stretch-artifact port ${STRETCH_ARTIFACT_PORT} is owned by another lane or suite; pick another`
	);
}

export const STRETCH_ARTIFACT_ORIGIN = `http://127.0.0.1:${STRETCH_ARTIFACT_PORT}`;

/** Where `adapter-static` writes the artifact under test. */
export const STRETCH_ARTIFACT_BUILD_DIR = `${FRONTEND_ROOT}build`;

export default defineConfig({
	testDir: '.',
	testMatch: 'stretch-artifact.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 60_000,
	globalTimeout: 600_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: {
		// Build first, then serve the output with a static server that applies
		// no transform of its own. `python3 -m http.server` is already the
		// static server this repo's desktop-setup gate uses, so no dependency
		// is added for this suite.
		command: guardedWebServerCommand('stretch-artifact-static', `pnpm build && python3 -m http.server ${STRETCH_ARTIFACT_PORT} --bind 127.0.0.1 --directory build`),
		cwd: FRONTEND_ROOT,
		url: `${STRETCH_ARTIFACT_ORIGIN}/index.html`,
		reuseExistingServer: false,
		timeout: 300_000
	},
	use: {
		baseURL: STRETCH_ARTIFACT_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'stretch-artifact-chromium',
			use: {
				...devices['Desktop Chrome'],
				// Without this a headless AudioContext stays suspended and the
				// processor never reaches its ready handshake.
				launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] }
			}
		},
		{
			name: 'stretch-artifact-webkit',
			use: { ...devices['Desktop Safari'] }
		}
	]
});
