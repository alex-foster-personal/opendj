/**
 * TIER 2: the REAL shell. Drives the packaged Tauri window's actual WKWebView.
 *
 * Tier 1 (apps/webui/frontend/tests/e2e/webkit-deckload.spec.ts) drives
 * Playwright's webkit against the engine-served production build. That catches
 * the whole WKWebView-engine fault class -- it is what caught the signalsmith
 * worklet defect that shipped broken -- but it is not the shell. What it cannot
 * see: the bootstrap page, the Rust initialization_script that injects
 * OPENDJ_ENGINE_ORIGIN, the navigation off tauri://localhost, window chrome,
 * and packaging. This tier covers exactly that residue.
 *
 * WHY THIS DRIVER. macOS has no WKWebView WebDriver, so `tauri-driver` supports
 * only Windows and Linux. `@wdio/tauri-service` with the EMBEDDED provider
 * compiles a W3C WebDriver server into the debug binary and drives the
 * WKWebView natively, returning results through a WKScriptMessageHandler
 * rather than Tauri IPC.
 *
 * The IPC distinction is the whole reason this driver and not the obvious
 * alternative: `tauri-plugin-playwright` returns every command result through
 * `window.__TAURI_INTERNALS__.invoke`, and Tauri v2 classifies this shell's
 * engine origin as Origin::Remote and DENIES IPC to it. That plugin would time
 * out on every command the moment setup.js navigates, and the only fix would be
 * a capability granting the http engine origin the right to invoke Tauri
 * commands -- an IPC hole punched through a shell whose entire thesis is that
 * Rust owns the window and nothing else.
 *
 * The plugin is a cfg(debug_assertions) dependency, so it cannot reach the dmg.
 *
 * Acceptance tests:
 *
 * - [if] the engine is not reachable [then] onPrepare exits non-zero before
 *   any window opens, rather than leaving a mystery timeout in the spec.
 * - [if] the shell never leaves tauri://localhost [then ⛔️] the smoke passes.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DESKTOP_ROOT = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = join(DESKTOP_ROOT, '..', '..');

/**
 * Tier 2 gets its OWN engine port and its OWN fixture dir. It must never share
 * either with tier 1 (8690) or any lane engine: the engine holds a singleton
 * lock on its data dir, so a shared dir means one of the two simply refuses to
 * boot. These ports are reserved elsewhere and must never be bound here:
 * 8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686,
 * 5399, 5311.
 */
const ENGINE_PORT = 8691;
const RESERVED_PORTS: readonly number[] = [
	8585, 5173, 8685, 9405, 8682, 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311, 8690
];
if (RESERVED_PORTS.includes(ENGINE_PORT)) {
	throw new Error(`wdio.conf: port ${ENGINE_PORT} is reserved by another suite`);
}

export const ENGINE_ORIGIN = `http://127.0.0.1:${ENGINE_PORT}`;

/** The embedded WebDriver server the debug binary hosts, kept off 4445 so a
 * stray default-port instance cannot be driven by accident. */
const EMBEDDED_WEBDRIVER_PORT = 4455;

const FIXTURE_DATA_DIR = join(DESKTOP_ROOT, 'tests', 'fixtures', 'real-shell-data');
const FIXTURE_BUILDER = join(
	REPOSITORY_ROOT,
	'apps/webui/frontend/tests/e2e/support/deckload_fixture.py'
);
const APP_BINARY = join(DESKTOP_ROOT, 'src-tauri', 'target', 'debug', 'opendj-desktop');

let engine: ChildProcess | null = null;

async function _waitForEngine(timeoutMs: number): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	let lastError = 'never attempted';
	while (Date.now() < deadline) {
		try {
			const response = await fetch(`${ENGINE_ORIGIN}/api/v1/health`);
			if (response.ok) return;
			lastError = `HTTP ${response.status}`;
		} catch (error) {
			lastError = String(error);
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(`engine never became healthy on ${ENGINE_ORIGIN}: ${lastError}`);
}

export const config: WebdriverIO.Config = {
	runner: 'local',
	specs: [join(DESKTOP_ROOT, 'tests', '*.e2e.ts')],
	maxInstances: 1,
	framework: 'mocha',
	reporters: ['spec'],
	mochaOpts: { ui: 'bdd', timeout: 180_000 },
	logLevel: 'warn',
	waitforTimeout: 30_000,
	connectionRetryCount: 0,

	services: [
		[
			'@wdio/tauri-service',
			{
				// macOS auto-detects this, but naming it makes the requirement
				// visible instead of implicit.
				driverProvider: 'embedded',
				embeddedPort: EMBEDDED_WEBDRIVER_PORT
			}
		]
	],

	capabilities: [
		{
			browserName: 'tauri',
			'tauri:options': { application: APP_BINARY }
		}
	] as unknown as WebdriverIO.Config['capabilities'],

	/** Boot a real engine over a real fixture library BEFORE any window opens. */
	onPrepare: async (): Promise<void> => {
		if (!existsSync(APP_BINARY)) {
			throw new Error(
				`real-shell e2e needs the DEBUG shell binary: ${APP_BINARY}\n` +
					`build it with: cd apps/desktop/src-tauri && cargo build`
			);
		}
		const built = spawn(
			'uv',
			['run', '--no-sync', 'python', FIXTURE_BUILDER, '--data-dir', FIXTURE_DATA_DIR],
			{ cwd: REPOSITORY_ROOT, stdio: 'inherit' }
		);
		const buildCode: number = await new Promise((resolve) => built.on('exit', resolve));
		if (buildCode !== 0) throw new Error(`fixture builder exited ${buildCode}`);

		engine = spawn(
			'uv',
			[
				'run',
				'--no-sync',
				'python',
				'-m',
				'apps.engine_core',
				'serve',
				'--data-dir',
				FIXTURE_DATA_DIR,
				'--host',
				'127.0.0.1',
				'--port',
				String(ENGINE_PORT)
			],
			{
				cwd: REPOSITORY_ROOT,
				stdio: 'inherit',
				env: { ...process.env, MDT_DATA_DIR: FIXTURE_DATA_DIR, WEB_CONCURRENCY: '' }
			}
		);
		await _waitForEngine(120_000);

		// The seam main.rs already documents: point the packaged shell at this
		// engine without rebuilding it. The service spawns the app as a child of
		// this process, so it inherits the variable.
		process.env.OPENDJ_ENGINE_ORIGIN = ENGINE_ORIGIN;
	},

	onComplete: async (): Promise<void> => {
		engine?.kill('SIGINT');
		engine = null;
	}
};
