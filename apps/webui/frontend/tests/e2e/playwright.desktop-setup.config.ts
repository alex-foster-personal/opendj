/**
 * Desktop shell setup screen: what a tester sees when the engine is down.
 *
 * The screen under test is NOT part of this SvelteKit app. It ships inside
 * the .app bundle (apps/desktop/setup) precisely because the SvelteKit app
 * is served BY the engine and therefore cannot load when the engine is
 * absent. Serving that same directory over http here exercises the real
 * shipped file, in a real browser, with no Tauri involved.
 *
 * Requirements:
 *
 * - ✔︎ Ports are this suite's own, never 8585/5173/8685/9405.
 * - ✔︎ The engine port is deliberately NEVER bound, so "unreachable" is a
 *   real condition rather than a stub.
 * - ✔︎ No retries and a hard timeout, so a hung gate fails fast.
 *
 * Acceptance tests:
 *
 * - [if] the static port is already bound [then ⛔️] the run starts.
 * - [if] something IS listening on the engine port [then ⛔️] the suite
 *   reports success, because it would be asserting the wrong branch.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));

/** This suite's own pair. Chosen clear of every reserved and e2e port. */
export const SETUP_PAGE_PORT = 5216;

/**
 * Held deliberately empty. The whole point of the suite is the engine
 * being absent, so this port must never have a server attached to it.
 */
export const DEAD_ENGINE_PORT = 9473;

export const SETUP_PAGE_ORIGIN = `http://127.0.0.1:${SETUP_PAGE_PORT}`;
export const DEAD_ENGINE_ORIGIN = `http://127.0.0.1:${DEAD_ENGINE_PORT}`;

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'desktop-setup.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 30_000,
	globalTimeout: 120_000,
	expect: { timeout: 10_000 },
	reporter: [['list']],
	webServer: [
		{
			// A plain static server, because that is all the shipped page
			// needs: no bundler, no framework, no engine.
			command: guardedWebServerCommand('desktop-setup-static', `uv run --no-project python -m http.server ${SETUP_PAGE_PORT} --bind 127.0.0.1`),
			cwd: `${REPOSITORY_ROOT}apps/desktop/setup`,
			url: `${SETUP_PAGE_ORIGIN}/index.html`,
			reuseExistingServer: false,
			timeout: 60_000
		}
	],
	use: {
		baseURL: SETUP_PAGE_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'desktop-setup-chromium',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } }
		}
	]
});
