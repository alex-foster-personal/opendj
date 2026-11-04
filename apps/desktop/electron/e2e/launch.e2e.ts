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

import { MAIN_MIN_W, PLAYER_W, STRIP_W, sidePlayerLayout } from '../src/side-player-layout';
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
	assert.deepEqual(seen.bridgeFns, [
		'applyUpdate',
		'exit',
		'listCoreAudioOutputs',
		'openExternal',
		'pickFolder',
		'relaunch',
		'setAudioEngineChoice'
	]);
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

// SPOT-6555b: the page's own bridge call, through the preload's ipcRenderer.invoke
// and the trusted main-process handler, docks a real player window beside
// Open DJ and loads the real open.spotify.com in it.
// No network intervention, so Spotify rejecting the
// view, its user agent or the navigation guard turns this red. With no route
// to Spotify it fails and says the capability is UNAVAILABLE.
type SideBridge = {
	open: (url: string, options?: { shrinkMain?: boolean }) => Promise<void>;
	setCollapsed: (collapsed: boolean) => Promise<void>;
	close: () => Promise<void>;
	importBrowserLogins: () => Promise<{ kind: string; reason?: string }>;
};

async function windowBounds(): Promise<{ main: Electron.Rectangle; player: Electron.Rectangle | null; count: number }> {
	return app.evaluate(({ BrowserWindow }) => {
		const all = BrowserWindow.getAllWindows();
		const main = all.find((w) => w.getParentWindow() === null);
		const player = all.find((w) => w.getParentWindow() !== null);
		if (main === undefined) throw new Error('no main window');
		return { main: main.getBounds(), player: player?.getBounds() ?? null, count: all.length };
	});
}

test('SPOT-6555b: the bridge docks a real Spotify player window and collapses and closes it', async () => {
	// Spotify's own public "Today's Top Hits" playlist.
	const url = 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M';
	try {
		const before = await windowBounds();
		assert.equal(before.count, 1);
		assert.equal(before.player, null);

		const kind = await page.evaluate(() => {
			const bridge = (globalThis as unknown as { opendjShell: { version: number; sidePlayer: SideBridge } }).opendjShell;
			return { version: bridge.version, fns: Object.keys(bridge.sidePlayer).sort() };
		});
		assert.deepEqual(kind, { version: 2, fns: ['close', 'importBrowserLogins', 'open', 'setCollapsed'] });

		await page.evaluate(
			(target) => (globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.open(target, { shrinkMain: true }),
			url
		);
		const open = await windowBounds();
		assert.equal(open.count, 2);
		assert.ok(open.player !== null);
		// The real windows land where the layout says for this display: docked at
		// Open DJ's edge when the screen has room, at the screen's edge over a
		// main window already at its minimum width when it does not.
		const workArea = await app.evaluate(({ screen }, bounds) => screen.getDisplayMatching(bounds).workArea, before.main);
		const expected = sidePlayerLayout({ main: before.main, workArea, collapsed: false, shrinkMain: true, restoreWidth: null });
		assert.deepEqual(open.main, expected.main);
		assert.deepEqual(open.player, expected.player);
		assert.equal(open.player.height, open.main.height);
		if (workArea.width >= MAIN_MIN_W + PLAYER_W) assert.equal(open.player.x, open.main.x + open.main.width);
		const readView = (): Promise<{ title?: string; url?: string; persistent?: boolean; loading?: boolean; pageTitle?: string }> =>
			app.evaluate(({ BrowserWindow }) => {
				const player = BrowserWindow.getAllWindows().find((w) => w.getParentWindow() !== null);
				const views = player?.contentView.children ?? [];
				const spotify = views.find(
					(v) => 'webContents' in v && (v as Electron.WebContentsView).webContents.getURL().startsWith('https://')
				);
				const wc = (spotify as Electron.WebContentsView | undefined)?.webContents;
				return {
					title: player?.getTitle(),
					url: wc?.getURL(),
					persistent: wc?.session.isPersistent(),
					loading: wc?.isLoading(),
					pageTitle: wc?.getTitle()
				};
			});
		const loaded = await waitFor(async () => {
			const v = await readView();
			return v.loading === false && /Spotify/.test(v.pageTitle ?? '');
		}, 45_000);
		const view = await readView();
		assert.ok(loaded, `UNAVAILABLE or rejected: open.spotify.com did not load in the player (${JSON.stringify(view)})`);
		assert.equal(view.title, 'Spotify');
		// Spotify served the playlist itself: no redirect off open.spotify.com.
		assert.equal(new URL(view.url ?? '').host, 'open.spotify.com');
		assert.equal(new URL(view.url ?? '').pathname, new URL(url).pathname);
		assert.equal(view.persistent, true);
		const readPage = (): Promise<{ ua: string; text: string }> =>
			app.evaluate(({ BrowserWindow }) => {
				const player = BrowserWindow.getAllWindows().find((w) => w.getParentWindow() !== null);
				const spotify = player?.contentView.children.find(
					(v) => 'webContents' in v && (v as Electron.WebContentsView).webContents.getURL().startsWith('https://')
				) as Electron.WebContentsView | undefined;
				return spotify?.webContents.executeJavaScript('({ ua: navigator.userAgent, text: document.body.innerText })') as Promise<{
					ua: string;
					text: string;
				}>;
			});
		// The web player renders the playlist itself, so Spotify served the app to
		// this view rather than an error or unsupported-browser page.
		const rendered = await waitFor(async () => /Today.s Top Hits/.test((await readPage()).text), 45_000);
		const seen = await readPage();
		assert.ok(rendered, `Spotify's web player never rendered the playlist: ${JSON.stringify(seen.text.slice(0, 400))}`);
		// Spotify's sign-in refuses Electron's own UA.
		assert.match(seen.ua, /Chrome\//);
		assert.doesNotMatch(seen.ua, /Electron/);
		// Spotify accepted the browser: its unsupported-browser page never shows.
		assert.doesNotMatch(seen.text, /browser (is not|isn't|is no longer) supported/i);

		// Permission checks are denied as well as requests.
		const notifications = await app.evaluate(({ BrowserWindow }) => {
			const player = BrowserWindow.getAllWindows().find((w) => w.getParentWindow() !== null);
			const spotify = player?.contentView.children.find(
				(v) => 'webContents' in v && (v as Electron.WebContentsView).webContents.getURL().startsWith('https://')
			) as Electron.WebContentsView | undefined;
			return spotify?.webContents.executeJavaScript(
				"navigator.permissions.query({ name: 'notifications' }).then((s) => s.state)"
			);
		});
		assert.equal(notifications, 'denied');
		const viewIds = await app.evaluate(({ BrowserWindow }) => {
			const player = BrowserWindow.getAllWindows().find((w) => w.getParentWindow() !== null);
			return (player?.contentView.children ?? [])
				.filter((v) => 'webContents' in v)
				.map((v) => (v as Electron.WebContentsView).webContents.id);
		});
		assert.equal(viewIds.length, 2);

		await page.evaluate(() => (globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.setCollapsed(true));
		const collapsed = await windowBounds();
		assert.equal(collapsed.player?.width, STRIP_W);
		assert.equal(collapsed.player?.x, collapsed.main.x + collapsed.main.width);

		// A URL off open.spotify.com is refused by the main process, not just the page.
		await assert.rejects(
			page.evaluate(() =>
				(globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.open('https://example.com/')
			)
		);

		await page.evaluate(() => (globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.close());
		assert.ok(await waitFor(async () => (await windowBounds()).count === 1));
		assert.deepEqual((await windowBounds()).main, before.main);
		// Closing ends both embedded renderers, not only their window.
		assert.ok(
			await waitFor(async () =>
				(await app.evaluate(({ webContents }, ids) => ids.every((id) => webContents.fromId(id) === undefined), viewIds))
			)
		);
		assert.ok(fs.readFileSync(shellLog, 'utf8').includes('side-player'));
	} finally {
		await page
			.evaluate(() => (globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.close())
			.catch(() => undefined);
	}

	// Off macOS the Chrome import says why instead of guessing (SPOT-6555c).
	if (process.platform !== 'darwin') {
		const outcome = await page.evaluate(() =>
			(globalThis as unknown as { opendjShell: { sidePlayer: SideBridge } }).opendjShell.sidePlayer.importBrowserLogins()
		);
		assert.notEqual(outcome.kind, 'imported');
		assert.ok(typeof outcome.reason === 'string' && outcome.reason.length > 0, JSON.stringify(outcome));
	}
});

test('the audio engine choice is saved to disk and handed to the next page load', async () => {
	const file = path.join(dataDir, 'audio-engine-choice.json');
	const scope = () => (globalThis as unknown as Record<string, unknown>).OPENDJ_AUDIO_ENGINE_CHOICE ?? null;
	// A fresh data dir has saved nothing, so the page gets no choice and uses its default.
	assert.equal(fs.existsSync(file), false);
	assert.equal(await page.evaluate(scope), null);
	await page.evaluate(() =>
		(globalThis as unknown as { opendjShell: { setAudioEngineChoice: (c: string) => Promise<void> } }).opendjShell.setAudioEngineChoice('webaudio')
	);
	assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { choice: 'webaudio' });
	await page.reload();
	assert.equal(await page.evaluate(scope), 'webaudio');
	// A value the page has no engine for is refused, and the saved one stays.
	const refused = await page.evaluate(() =>
		(globalThis as unknown as { opendjShell: { setAudioEngineChoice: (c: string) => Promise<void> } }).opendjShell
			.setAudioEngineChoice('tauri')
			.then(() => 'saved', (error: unknown) => String(error))
	);
	assert.match(refused, /must be webaudio\|rust/);
	assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { choice: 'webaudio' });
});

test('will-navigate blocks off-origin main-frame navigation and keeps one BrowserWindow', async () => {
	const before = page.url();
	// Not loopback, so not trusted; TEST-NET-1, so nothing real is contacted.
	await page.evaluate(() => {
		globalThis.location.href = 'http://192.0.2.1/';
	});
	assert.ok(await waitFor(() => fs.readFileSync(shellLog, 'utf8').includes('refused navigation to http://192.0.2.1/')));
	await page.waitForTimeout(500);
	assert.equal(page.url(), before);
	assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
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
