import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

import { claimAndCheckWebuiDevConfigOnce } from './webui-port-config';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
// Once = claim/check in the main process only; workers reuse its payload
// (they re-import this config after vite has bound the claimed port).
const ports = claimAndCheckWebuiDevConfigOnce(REPOSITORY_ROOT, 'frontend');

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
		'**/performance-*.spec.ts', // playwright.performance.config.ts (real library)
		'**/savepoint-smoke.spec.ts', // playwright.savepoint.config.ts (real library)
		'**/webkit-deckload.spec.ts', // playwright.webkit-deckload.config.ts (built artifact)
		'**/deckload-smoke.spec.ts', // playwright.webkit-deckload.config.ts (built artifact, chromium+webkit, #770)
		'**/desktop-setup.spec.ts', // playwright.desktop-setup.config.ts (static http.server)
		'**/play-analytics.spec.ts', // playwright.play-analytics.config.ts (fixture server)
		'**/rekordbox-writeback-disabled.spec.ts', // playwright.rekordbox-gate.config.ts
		'**/hot-cue-mapping-gate.spec.ts', // playwright.hotcue-mapping-gate.config.ts (real backend, fixture library)
		'**/stems-progress.spec.ts', // playwright.stems.config.ts (engine + ffmpeg)
		'**/stretch-artifact.spec.ts', // playwright.stretch-artifact.config.ts (built artifact)
		'**/stretch-quality.spec.ts' // playwright.stretch-quality.config.ts (no server)
	],
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
