/**
 * One `test` for two shells: a Playwright browser, or the Electron desktop
 * shell (ELECTRON-6208).
 *
 * The artifact suites (setup-entry-points, deckload-smoke) import `test` and
 * `expect` from here instead of from `@playwright/test`. Under every browser
 * config this module IS `@playwright/test`: the export below is the base
 * `test` object, untouched, so the webkit and chromium gates run exactly as
 * before.
 *
 * Under playwright.electron-shell.config.ts (which sets
 * OPENDJ_E2E_SHELL=electron before any worker loads a spec) the `context` and
 * `page` fixtures are replaced. Each test launches the real Electron shell
 * (`apps/desktop/electron/dist/main.js`) with Playwright's `_electron`
 * launcher, pointed through the shell's own `OPENDJ_ENGINE_ORIGIN` test seam
 * at the engine the config's webServer started over the throwaway fixture
 * library. The page a spec receives is the shell's one BrowserWindow, after
 * the shell's bootstrap page has handed off to that engine origin, with the
 * preload, the native bridge, the permission handler and the navigation
 * guard all live. Nothing in the page is stubbed.
 *
 * Isolation matches a browser context: every test gets a fresh scratch
 * HOME/XDG_CONFIG_HOME (so Electron's userData, its localStorage and its
 * single-instance lock are new each time, and no real profile or library is
 * reachable) and a fresh shell data dir. The engine and its fixture library
 * are shared across tests, exactly as the webkit config shares them.
 *
 * Electron contexts have no `baseURL` option, so this module resolves the
 * relative URLs specs pass to `page.goto` and `page.request.*` against the
 * engine origin. That is the only adaptation; assertions are unchanged.
 */
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
	_electron,
	expect,
	test as base,
	type APIRequestContext,
	type BrowserContext,
	type ElectronApplication,
	type Page
} from '@playwright/test';

export { expect };

/** The env switch playwright.electron-shell.config.ts sets. */
export const SHELL_ENV = 'OPENDJ_E2E_SHELL';
/** Engine origin the Electron config's webServer serves. */
export const ELECTRON_ENGINE_ORIGIN_ENV = 'OPENDJ_E2E_ENGINE_ORIGIN';
/** Absolute path of apps/desktop/electron (the app dir Electron is launched on). */
export const ELECTRON_APP_DIR_ENV = 'OPENDJ_E2E_ELECTRON_APP_DIR';
/** Absolute path of the Electron binary. */
export const ELECTRON_BINARY_ENV = 'OPENDJ_E2E_ELECTRON_BINARY';

function required(name: string): string {
	const value = process.env[name];
	if (value === undefined || value.trim() === '') {
		throw new Error(`${name} is not set; run this suite through playwright.electron-shell.config.ts`);
	}
	return value;
}

function resolveAgainst(origin: string, url: string): string {
	return new URL(url, origin).toString();
}

const REQUEST_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'fetch'] as const;

/** Make `request.get('/api/...')` resolve against the engine, as a baseURL would. */
function rebaseRequest(request: APIRequestContext, origin: string): void {
	const target = request as unknown as Record<string, (url: unknown, options?: unknown) => unknown>;
	for (const method of REQUEST_METHODS) {
		const original = target[method].bind(request);
		target[method] = (url: unknown, options?: unknown) =>
			original(typeof url === 'string' ? resolveAgainst(origin, url) : url, options);
	}
}

/** Make `page.goto('/performance')` resolve against the engine, as a baseURL would. */
function rebasePage(page: Page, origin: string): void {
	const goto = page.goto.bind(page);
	(page as unknown as { goto: Page['goto'] }).goto = (url, options) =>
		goto(resolveAgainst(origin, url), options);
	rebaseRequest(page.request, origin);
}

async function launchShell(origin: string): Promise<{ app: ElectronApplication; scratch: string }> {
	const appDir = required(ELECTRON_APP_DIR_ENV);
	const executablePath = required(ELECTRON_BINARY_ENV);
	const mainJs = join(appDir, 'dist', 'main.js');
	if (!existsSync(mainJs)) {
		throw new Error(`${mainJs} is missing; run \`pnpm build\` in ${appDir} first`);
	}
	const scratch = mkdtempSync(join(tmpdir(), 'odj-electron-e2e-'));
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) if (value !== undefined) env[key] = value;
	delete env.ELECTRON_RUN_AS_NODE;
	delete env.OPENDJ_PAYLOAD_DIR;
	env.OPENDJ_ENGINE_ORIGIN = origin;
	env.OPENDJ_SHELL_DATA_DIR = join(scratch, 'shell-data');
	// The page defaults to the Rust audio engine under the shell, and a CI runner has
	// no output device for odj-audio (it exits and the deck errors). These specs prove
	// the shell's handoff, prefs and deck load, not the device path, so the profile
	// starts on Web Audio, written where the shell persists the choice.
	mkdirSync(env.OPENDJ_SHELL_DATA_DIR, { recursive: true });
	writeFileSync(join(env.OPENDJ_SHELL_DATA_DIR, 'audio-engine-choice.json'), JSON.stringify({ choice: 'webaudio' }));
	// Electron derives userData (localStorage, the single-instance lock) from
	// these, so each launch is a fresh profile and never the user's own.
	env.HOME = join(scratch, 'home');
	env.XDG_CONFIG_HOME = join(scratch, 'config');
	env.OPENDJ_TEST_RUN = '1';
	// Chromium refuses to run as root with its sandbox on; CI containers are root.
	const args = process.getuid?.() === 0 ? ['--no-sandbox', appDir] : [appDir];
	const app = await _electron.launch({ executablePath, args, cwd: appDir, env, timeout: 60_000 });
	return { app, scratch };
}

type ElectronFixtures = { electronApp: ElectronApplication };

const electronTest = base.extend<ElectronFixtures>({
	electronApp: async ({}, use, testInfo) => {
		const { app, scratch } = await launchShell(required(ELECTRON_ENGINE_ORIGIN_ENV));
		// The runner's own `trace` option only covers contexts it created, so the
		// shell's context is traced here and kept only when the test failed.
		await app.context().tracing.start({ screenshots: true, snapshots: true, sources: true });
		try {
			await use(app);
		} finally {
			const failed = testInfo.status !== testInfo.expectedStatus;
			const tracePath = testInfo.outputPath('electron-trace.zip');
			await app
				.context()
				.tracing.stop(failed ? { path: tracePath } : undefined)
				.catch(() => undefined);
			if (failed) await testInfo.attach('electron-trace', { path: tracePath }).catch(() => undefined);
			// exit through the shell's own path; close() alone would wait on the
			// page's quit gate, which a test may have left armed.
			await app.evaluate(({ app: electron }) => electron.exit(0)).catch(() => undefined);
			await app.close().catch(() => undefined);
			// One scratch profile per test: remove it, or a long run leaves a tree per test in tmpdir.
			rmSync(scratch, { recursive: true, force: true });
		}
	},
	context: async ({ electronApp }, use) => {
		await use(electronApp.context() as BrowserContext);
	},
	page: async ({ electronApp }, use) => {
		const origin = required(ELECTRON_ENGINE_ORIGIN_ENV);
		const page = await electronApp.firstWindow();
		// The shell opens its bootstrap page first and hands off to the engine;
		// a spec's own goto must not race that hand-off.
		await page.waitForURL((url) => url.origin === origin, { timeout: 30_000 });
		await electronApp.evaluate(({ BrowserWindow }) => {
			BrowserWindow.getAllWindows()[0]?.setContentSize(1600, 1000);
		});
		rebasePage(page, origin);
		await use(page);
	}
});

export const test: typeof base = process.env[SHELL_ENV] === 'electron' ? (electronTest as unknown as typeof base) : base;
