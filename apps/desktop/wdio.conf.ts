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
import { existsSync, mkdirSync } from 'node:fs';
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

/**
 * The seam main.rs documents: point the shell at this engine without
 * rebuilding it.
 *
 * Set HERE, at module scope, and deliberately not in `onPrepare`. The app is
 * spawned by the service inside the WDIO WORKER process, while `onPrepare`
 * runs in the launcher, so an assignment there only reaches the app if the
 * launcher's environment happens to be inherited intact through the fork. It
 * was, for six consecutive runs, and then it was not: the shell fell back to
 * its baked default (:8685) and attached to whatever engine was already
 * listening there. WDIO loads this config in every process, so assigning at
 * module scope reaches the process that actually spawns the app. Test 1
 * asserts the injected value for exactly this reason.
 */
process.env.OPENDJ_ENGINE_ORIGIN = ENGINE_ORIGIN;

/** The embedded WebDriver server the debug binary hosts, kept off 4445 so a
 * stray default-port instance cannot be driven by accident. */
const EMBEDDED_WEBDRIVER_PORT = 4455;

const FIXTURE_DATA_DIR = join(DESKTOP_ROOT, 'tests', 'fixtures', 'real-shell-data');

/**
 * HOME for the fixture engine, and the incident that put it here.
 *
 * apps/shared/platform_paths.py derives the rekordbox app dir
 * (~/Library/Pioneer/rekordbox) and the default music root (~/Music) from
 * HOME. On Wed 19 Aug 2026 a `setup.import-rekordbox` job ran against THIS
 * fixture dir, snapshotted and decrypted the live master.db into it, and wrote
 * 32 real tracks plus a playlist into a library that is supposed to hold two
 * generated tones. Nothing was written toward rekordbox, and the builder's
 * count check failed the next run loudly, but a fixture whose isolation
 * depends on nobody triggering the wizard is not isolated. With HOME here,
 * that import can only ever find an empty directory.
 */
const SANDBOX_HOME = join(FIXTURE_DATA_DIR, 'sandbox-home');
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
		mkdirSync(SANDBOX_HOME, { recursive: true });
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
				env: {
					...process.env,
					MDT_DATA_DIR: FIXTURE_DATA_DIR,
					WEB_CONCURRENCY: '',
					HOME: SANDBOX_HOME
				}
			}
		);
		await _waitForEngine(120_000);
	},

	/**
	 * Re-check the engine INSIDE the worker, because a failed `onPrepare` does
	 * not stop the run. WDIO logs the hook error and launches the specs anyway,
	 * which is how a fixture builder that exited 1 turned into three unrelated
	 * assertion failures against a shell that had quietly fallen back to its
	 * baked default port. One honest error beats three misleading ones.
	 */
	before: async (): Promise<void> => {
		const response = await fetch(`${ENGINE_ORIGIN}/api/v1/health`).catch(
			(error: unknown) => error
		);
		if (!(response instanceof Response) || !response.ok) {
			throw new Error(
				`no fixture engine on ${ENGINE_ORIGIN}: ${String(response)}. onPrepare ` +
					'failed (its error is above this line) and WDIO started the specs anyway.'
			);
		}
	},

	onComplete: async (): Promise<void> => {
		engine?.kill('SIGINT');
		engine = null;
	}
};
