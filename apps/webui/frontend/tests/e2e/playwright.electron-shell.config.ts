/**
 * Electron shell gate (ELECTRON-6208): the artifact smoke specs, driven through
 * the real Electron desktop shell instead of a Playwright browser.
 *
 * WHY. The WebKit gate (playwright.webkit-deckload.config.ts) exists because
 * the packaged app was a WKWebView. The desktop app is moving to Electron
 * (apps/desktop/electron), and WebKit caused 11 of the 15 real merge-queue
 * failures in the 8 Oct 2026 PR-flow research. This config runs the same
 * named subset the WebKit gate blocks on, inside the shell users will run:
 * Electron's Chromium, its preload and native bridge, its permission handler
 * and its navigation guard, against the PRODUCTION build served by the
 * ENGINE. It sits BESIDE the WebKit gate; retiring that gate is the owner's
 * call once this one has a green record (see e2e.yml, job `electron`).
 *
 * HOW. The webServer below boots the real engine over a throwaway fixture
 * library, built by the same generator and real folder ingest the WebKit
 * gate uses (support/deckload_fixture.py), in its own data dir. Each test
 * then launches `apps/desktop/electron/dist/main.js` with Playwright's
 * `_electron` launcher, pointed at that engine through the shell's
 * OPENDJ_ENGINE_ORIGIN test seam (support/shell-test.ts). The shell runs the
 * real bootstrap page, which hands off to the engine's /performance. No
 * route is stubbed and no response is faked.
 *
 * WHAT RUNS. The two greps the WebKit gate blocks on, over the same files:
 *   - setup-entry-points.spec.ts, `setup entry points`
 *   - deckload-smoke.spec.ts, `deck-load smoke` (CI passes no grep, so this
 *     config runs every test in both files except the one named below)
 * NAMED EXCLUSION, not a silent skip: `the build identity chip states this
 * app address in its foldout` asserts that a target=_blank link opens a
 * second browser tab. The Electron shell refuses every new window by design
 * (src/main.ts setWindowOpenHandler: deny, https goes to the system browser),
 * so that assertion is about browser tabs, not this shell. A shell-specific
 * replacement belongs in apps/desktop/electron/e2e.
 *
 * Requirements:
 *
 * - One fixed loopback port (8694), reserved in apps/webui/port_config.py.
 * - Fixture data dir inside this worktree, never a shared or real one; the
 *   engine and every shell launch get a sandbox HOME.
 * - The production build and the Electron dist must already exist; an absent
 *   one fails at config load with the command to run.
 * - No retries and one worker.
 *
 * Run (Linux needs a display; a Mac does not):
 *
 *   pnpm build                                   # apps/webui/frontend
 *   pnpm build                                   # apps/desktop/electron
 *   xvfb-run -a -s "-screen 0 1920x1200x24" \
 *     pnpm exec playwright test --config tests/e2e/playwright.electron-shell.config.ts
 */
import { defineConfig } from '@playwright/test';
import { existsSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { guardedWebServerCommand } from './support/guarded-web-server';
import {
	ELECTRON_APP_DIR_ENV,
	ELECTRON_BINARY_ENV,
	ELECTRON_ENGINE_ORIGIN_ENV,
	SHELL_ENV
} from './support/shell-test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const ELECTRON_APP_DIR = join(REPOSITORY_ROOT, 'apps', 'desktop', 'electron');

/** This suite's own port. Fixed, so two concurrent runs collide loudly. */
export const ELECTRON_SHELL_PORT = 8694;
export const ELECTRON_SHELL_ORIGIN = `http://127.0.0.1:${ELECTRON_SHELL_PORT}`;

/** Throwaway library the engine serves. Inside this worktree, gitignored. */
export const FIXTURE_DATA_DIR = join(FRONTEND_ROOT, 'tests', 'e2e', 'fixtures', 'electron-shell-data');

/** HOME for the engine, so no default path can resolve to a real library. */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
mkdirSync(SANDBOX_HOME, { recursive: true });

const BUILD_INDEX = join(FRONTEND_ROOT, 'build', 'index.html');
if (!existsSync(BUILD_INDEX)) {
	throw new Error(
		`electron-shell needs the production build: ${BUILD_INDEX} does not exist. ` +
			'Run `pnpm build` in apps/webui/frontend first. This suite deliberately does NOT use vite.'
	);
}
const ELECTRON_MAIN = join(ELECTRON_APP_DIR, 'dist', 'main.js');
if (!existsSync(ELECTRON_MAIN)) {
	throw new Error(`electron-shell needs ${ELECTRON_MAIN}: run \`pnpm install && pnpm build\` in ${ELECTRON_APP_DIR}.`);
}
// The `electron` package's main export is the path of its binary.
const ELECTRON_BINARY = createRequire(join(ELECTRON_APP_DIR, 'package.json'))('electron') as string;
if (typeof ELECTRON_BINARY !== 'string' || !existsSync(ELECTRON_BINARY)) {
	throw new Error(
		`electron-shell: the Electron binary is missing (${String(ELECTRON_BINARY)}); ` +
			`run \`pnpm install\` in ${ELECTRON_APP_DIR}.`
	);
}

// Workers re-evaluate this config, so these reach support/shell-test.ts
// before any spec module loads.
process.env[SHELL_ENV] = 'electron';
process.env[ELECTRON_ENGINE_ORIGIN_ENV] = ELECTRON_SHELL_ORIGIN;
process.env[ELECTRON_APP_DIR_ENV] = ELECTRON_APP_DIR;
process.env[ELECTRON_BINARY_ENV] = ELECTRON_BINARY;

const ENGINE_COMMAND = [
	`uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture --data-dir ${FIXTURE_DATA_DIR} --seed-playlists`,
	'&&',
	'uv run --no-sync python -m apps.engine_core serve',
	`--data-dir ${FIXTURE_DATA_DIR}`,
	'--host 127.0.0.1',
	`--port ${ELECTRON_SHELL_PORT}`
].join(' ');

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	// Named individually, never a glob (same reason as the webkit config).
	testMatch: ['setup-entry-points.spec.ts', 'deckload-smoke.spec.ts'],
	grepInvert: /the build identity chip states this app address in its foldout/,
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	globalTimeout: 900_000,
	expect: { timeout: 20_000 },
	reporter: [['list']],
	webServer: {
		command: guardedWebServerCommand('electron-shell-engine', ENGINE_COMMAND),
		cwd: REPOSITORY_ROOT,
		url: `${ELECTRON_SHELL_ORIGIN}/api/v1/health`,
		reuseExistingServer: false,
		timeout: 180_000,
		env: {
			...process.env,
			MDT_DATA_DIR: FIXTURE_DATA_DIR,
			WEB_CONCURRENCY: '',
			HOME: SANDBOX_HOME
		}
	},
	use: {
		// For the `request` fixture; pages come from the shell, not a browser.
		baseURL: ELECTRON_SHELL_ORIGIN,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'off'
	},
	projects: [{ name: 'electron' }]
});
