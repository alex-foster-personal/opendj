// The real Electron app, launched the way a user launches it, against a fake
// engine payload in a scratch data dir. Runs under xvfb-run on Linux.
//
// What this proves that the unit tests cannot: the preload really injects the
// globals before page scripts, the bridge really refuses an untrusted page,
// window close really goes through the page's quit hook, and quitting really
// stops the engine's process group.

import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as path from 'node:path';
import { after, before, test } from 'node:test';

import { _electron, type ElectronApplication, type Page } from '@playwright/test';

import { fakePayload, pidGone, scratchDir, waitFor } from '../test/helpers';

const APP_DIR = path.resolve(__dirname, '..', '..');

function getJson(port: number, route: string): Promise<Record<string, unknown>> {
	return new Promise((resolve, reject) => {
		http
			.get({ host: '127.0.0.1', port, path: route }, (res) => {
				let body = '';
				res.setEncoding('utf8');
				res.on('data', (chunk: string) => (body += chunk));
				res.on('end', () => {
					try {
						resolve(JSON.parse(body) as Record<string, unknown>);
					} catch (error) {
						reject(new Error(`${route} answered ${res.statusCode} with non-JSON: ${body} (${String(error)})`));
					}
				});
			})
			.on('error', reject);
	});
}

/**
 * Shell health once the supervisor's first poll has filled in the engine's
 * pid and port. Until then the snapshot is the default (`running`, no lock
 * fields), exactly as in the Tauri shell.
 */
async function polledHealth(): Promise<Record<string, unknown>> {
	const { health_port: healthPort } = JSON.parse(fs.readFileSync(path.join(dataDir, '.engine.shell.json'), 'utf8')) as {
		health_port: number;
	};
	let health: Record<string, unknown> = {};
	const polled = await waitFor(async () => {
		health = await getJson(healthPort, '/api/v1/health');
		return typeof health.lock_pid === 'number';
	}, 10_000);
	assert.ok(polled, `the supervisor never published the engine pid: ${JSON.stringify(health)}`);
	return health;
}

let app: ElectronApplication;
let page: Page;
let dataDir: string;
let shellLog: string;

before(async () => {
	const dir = scratchDir('e2e');
	dataDir = path.join(dir, 'data');
	shellLog = path.join(dataDir, 'logs', 'engine.log'); // the shell logs beside the engine, as the Tauri shell did
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) if (value !== undefined) env[key] = value;
	delete env.OPENDJ_ENGINE_ORIGIN;
	delete env.ELECTRON_RUN_AS_NODE;
	env.OPENDJ_PAYLOAD_DIR = fakePayload(dir);
	env.OPENDJ_SHELL_DATA_DIR = dataDir;
	// Chromium refuses to run as root with its sandbox on; CI containers are root.
	const args = process.getuid?.() === 0 ? ['--no-sandbox', APP_DIR] : [APP_DIR];
	app = await _electron.launch({ args, cwd: APP_DIR, env, timeout: 60_000 });
	page = await app.firstWindow();
	await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\/performance$/, { timeout: 30_000 });
});

after(async () => {
	// Normally the quit test already ended the app.
	await app?.close().catch(() => undefined);
});

test('the bootstrap page hands off to the engine origin', async () => {
	assert.equal(await page.textContent('#fake'), 'fake engine page');
});

test('the page sees the shell globals and the bridge, and no Node', async () => {
	const seen = await page.evaluate(() => {
		const scope = globalThis as unknown as Record<string, unknown>;
		const bridge = scope.opendjShell as Record<string, unknown> | undefined;
		return {
			origin: scope.OPENDJ_ENGINE_ORIGIN,
			shell: (scope.OPENDJ_SHELL_BUILD as Record<string, unknown> | undefined)?.shell,
			quitHook: typeof scope.__OPENDJ_requestQuit,
			pending: Array.isArray(scope.__OPENDJ_PENDING_SHELL_ERRORS__),
			bridgeKind: bridge?.kind,
			bridgeFns: bridge === undefined ? [] : Object.keys(bridge).filter((key) => typeof bridge[key] === 'function').sort(),
			require: typeof scope.require,
			process: typeof scope.process
		};
	});
	assert.equal(seen.origin, new URL(page.url()).origin);
	assert.equal(seen.shell, 'electron');
	assert.equal(seen.quitHook, 'function');
	assert.equal(seen.pending, true);
	assert.equal(seen.bridgeKind, 'electron');
	assert.deepEqual(seen.bridgeFns, ['applyUpdate', 'exit', 'openExternal', 'pickFolder', 'relaunch']);
	assert.equal(seen.require, 'undefined');
	assert.equal(seen.process, 'undefined');
});

test('the shell publishes its health port and reports the engine running', async () => {
	const shellJson = path.join(dataDir, '.engine.shell.json');
	assert.equal(fs.statSync(shellJson).mode & 0o777, 0o600);
	const parent = fs.readFileSync(path.join(dataDir, '.engine.parent'), 'utf8').trim();
	assert.match(parent, /^\d+$/);
	const health = await polledHealth();
	assert.equal(health.engine, 'running');
	assert.equal(health.lock_port, Number(new URL(page.url()).port));
});

test('a navigation to an untrusted URL is refused and the page stays put', async () => {
	const before = page.url();
	// Not loopback, so not trusted; TEST-NET-1, so nothing real is contacted.
	await page.evaluate(() => {
		globalThis.location.href = 'http://192.0.2.1/';
	});
	assert.ok(await waitFor(() => fs.readFileSync(shellLog, 'utf8').includes('refused navigation to http://192.0.2.1/')));
	await page.waitForTimeout(500);
	assert.equal(page.url(), before);
});

test('an untrusted page gets no globals and no bridge', async () => {
	const engineUrl = page.url();
	await app.evaluate(async ({ BrowserWindow }) => {
		await BrowserWindow.getAllWindows()[0]?.loadURL('data:text/html,<p id="x">untrusted</p>');
	});
	const seen = await page.evaluate(() => {
		const scope = globalThis as unknown as Record<string, unknown>;
		return { bridge: typeof scope.opendjShell, origin: typeof scope.OPENDJ_ENGINE_ORIGIN };
	});
	assert.deepEqual(seen, { bridge: 'undefined', origin: 'undefined' });
	assert.ok(await waitFor(() => fs.readFileSync(shellLog, 'utf8').includes('refused native bridge call from data:')));
	await app.evaluate(async ({ BrowserWindow }, url) => {
		await BrowserWindow.getAllWindows()[0]?.loadURL(url);
	}, engineUrl);
	assert.equal(await page.evaluate(() => typeof (globalThis as unknown as Record<string, unknown>).opendjShell), 'object');
});

test('closing the window asks the page, and does not quit on its own', async () => {
	await page.evaluate(() => {
		(globalThis as unknown as Record<string, unknown>).__OPENDJ_requestQuit = () => {
			(globalThis as unknown as Record<string, unknown>).quitAsked = true;
		};
	});
	await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.close());
	assert.ok(await waitFor(async () => (await page.evaluate(() => (globalThis as unknown as Record<string, unknown>).quitAsked)) === true));
	assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
});

test('exit through the bridge quits the app and stops the engine', async () => {
	const enginePid = (await polledHealth()).lock_pid as number;
	assert.ok(enginePid > 0 && !pidGone(enginePid), 'the engine is alive before quitting');
	const closed = new Promise<void>((resolve) => app.process().once('exit', () => resolve()));
	await page.evaluate(() => (globalThis as unknown as { opendjShell: { exit: (code: number) => Promise<void> } }).opendjShell.exit(0)).catch(() => undefined);
	await closed;
	assert.ok(await waitFor(() => pidGone(enginePid)), 'the engine exits with the app');
	const log = fs.readFileSync(shellLog, 'utf8');
	assert.match(log, /shell exit: /);
});
