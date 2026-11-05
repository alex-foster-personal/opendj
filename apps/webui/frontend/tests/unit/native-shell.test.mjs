/**
 * The shell-neutral native bridge (apps/webui/frontend/src/lib/shell/native-shell.ts).
 *
 * - if an Electron page is read as a browser tab then the folder picker,
 *   sign-in, quit and update all silently vanish in the Electron app -> broken
 * - if a browser tab is read as a shell then a tab offers controls that throw
 *   -> broken (the over-detection control)
 * - if a lookalike global (wrong kind, null) counts as Electron then any page
 *   script can impersonate the shell -> broken
 * - if the Electron branch is not preferred when both globals exist then a
 *   stray Tauri global reroutes calls to a dead plugin -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let side;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/shell/native-shell.ts');
	side = await loadTypeScriptModule('src/lib/shell/side-player-shell.ts');
});

function electronScope(overrides = {}) {
	const calls = [];
	const bridge = {
		kind: 'electron',
		version: 1,
		pickFolder: async (options) => {
			calls.push(['pickFolder', options]);
			return '/Users/dev/Music';
		},
		openExternal: async (url) => {
			calls.push(['openExternal', url]);
		},
		exit: async (code) => {
			calls.push(['exit', code]);
		},
		relaunch: async () => {
			calls.push(['relaunch']);
		},
		applyUpdate: async (onProgress) => {
			onProgress({ phase: 'checking' });
			calls.push(['applyUpdate']);
			return { kind: 'no-update' };
		},
		listCoreAudioOutputs: async () => {
			calls.push(['listCoreAudioOutputs']);
			return [{ uid: 'hp' }];
		},
		...overrides
	};
	return { scope: { opendjShell: bridge }, calls };
}

test('a browser tab has no native shell', () => {
	assert.equal(mod.nativeShellKind({}), null);
});

test('the Electron preload bridge is detected', () => {
	assert.equal(mod.nativeShellKind(electronScope().scope), 'electron');
});

test('the Tauri global is still detected until cutover', () => {
	assert.equal(mod.nativeShellKind({ __TAURI_INTERNALS__: {} }), 'tauri');
});

test('a lookalike opendjShell is not the Electron shell', () => {
	assert.equal(mod.nativeShellKind({ opendjShell: null }), null);
	assert.equal(mod.nativeShellKind({ opendjShell: { kind: 'tauri' } }), null);
	assert.equal(mod.nativeShellKind({ opendjShell: 'electron' }), null);
});

test('Electron wins when both globals are present', () => {
	const { scope } = electronScope();
	scope.__TAURI_INTERNALS__ = {};
	assert.equal(mod.nativeShellKind(scope), 'electron');
});

test('pickFolder, openExternal and exitApp go through the Electron bridge', async () => {
	const { scope, calls } = electronScope();
	assert.equal(await mod.pickFolder('Choose a folder', scope), '/Users/dev/Music');
	await mod.openExternal('https://accounts.google.com/o/oauth2/auth', scope);
	await mod.exitApp(0, scope);
	assert.deepEqual(calls, [
		['pickFolder', { title: 'Choose a folder' }],
		['openExternal', 'https://accounts.google.com/o/oauth2/auth'],
		['exit', 0]
	]);
});

test('a cancelled Electron picker is null, not an empty path', async () => {
	const { scope } = electronScope({ pickFolder: async () => null });
	assert.equal(await mod.pickFolder('Choose a folder', scope), null);
});

test('every native call outside a shell fails loud instead of doing nothing', async () => {
	await assert.rejects(mod.pickFolder('x', {}), /browser tab/);
	await assert.rejects(mod.openExternal('https://example.com', {}), /browser tab/);
	await assert.rejects(mod.exitApp(0, {}), /browser tab/);
});

test('electronApplyUpdate is null outside Electron so callers keep their own path', () => {
	assert.equal(
		mod.electronApplyUpdate(() => {}, {}),
		null
	);
	assert.equal(
		mod.electronApplyUpdate(() => {}, { __TAURI_INTERNALS__: {} }),
		null
	);
});

test('electronApplyUpdate forwards progress and the outcome', async () => {
	const { scope } = electronScope();
	const seen = [];
	const outcome = await mod.electronApplyUpdate((p) => seen.push(p.phase), scope);
	assert.deepEqual(outcome, { kind: 'no-update' });
	assert.deepEqual(seen, ['checking']);
});

// ----- SPOT-6555b/03: the Spotify side player -----------------------------------
// These pin the page-side adapter's routing only. The real preload, IPC
// handler and player window are driven in apps/desktop/electron/e2e/launch.e2e.ts.
test('SPOT-6555b: Electron v2 docks the player; options pass through', async () => {
	const calls = [];
	const scope = {
		opendjShell: {
			kind: 'electron',
			version: 2,
			sidePlayer: {
				open: async (url, options) => calls.push(['open', url, options]),
				setCollapsed: async () => {},
				close: async () => {},
				importBrowserLogins: async () => ({
					kind: 'imported',
					browser: 'Google Chrome',
					cookies: 3,
					sites: ['spotify.com']
				})
			}
		}
	};
	assert.equal(side.hasSidePlayer(scope), true);
	await side.openSpotifySidePlayer(
		'https://open.spotify.com/playlist/abc',
		{ shrinkMain: false },
		scope
	);
	assert.deepEqual(calls, [
		['open', 'https://open.spotify.com/playlist/abc', { shrinkMain: false }]
	]);
	assert.equal((await side.importBrowserLogins(scope)).cookies, 3);
});

test('SPOT-6555b: a v1 Electron opens the player through its own openExternal', async () => {
	// Electron's main window denies every new window, so window.open there
	// returns null and would read as a blocked pop-up.
	const external = [];
	const popups = [];
	const scope = {
		opendjShell: { kind: 'electron', version: 1, openExternal: async (url) => external.push(url) },
		open: (...args) => (popups.push(args), null)
	};
	assert.equal(side.hasSidePlayer(scope), false);
	await side.openSpotifySidePlayer('https://open.spotify.com/playlist/abc', {}, scope);
	assert.deepEqual(external, ['https://open.spotify.com/playlist/abc']);
	assert.deepEqual(popups, []);
	assert.equal((await side.importBrowserLogins(scope)).kind, 'refused');
});

test('SPOT-6555b: a browser tab opens a disowned pop-up instead', async () => {
	const opened = [];
	const player = { opener: 'the Open DJ tab', location: { href: 'about:blank' } };
	const scope = {
		// Stricter than Window.open, which Chromium 141 runs on an undefined
		// receiver but refuses on a foreign one ("Illegal invocation"): the
		// fallback must call it on its own scope.
		open(...args) {
			if (this !== scope) throw new TypeError('Illegal invocation');
			opened.push(args);
			return player;
		}
	};
	assert.equal(side.hasSidePlayer(scope), false);
	await side.openSpotifySidePlayer('https://open.spotify.com/playlist/abc', {}, scope);
	// Opened blank, disowned, then pointed at Spotify.
	assert.deepEqual(opened[0].slice(0, 2), ['', '_blank']);
	assert.equal(player.opener, null);
	assert.equal(player.location.href, 'https://open.spotify.com/playlist/abc');
	const outcome = await side.importBrowserLogins(scope);
	assert.equal(outcome.kind, 'refused');
});

test('SPOT-6555b: Tauri opens the player in the system browser, not a swallowed pop-up', async () => {
	// Tauri's window has no new-window handler, so window.open there opens
	// nothing; its opener plugin is the working route.
	const external = [];
	const popups = [];
	const scope = { __TAURI_INTERNALS__: {}, open: (...args) => (popups.push(args), null) };
	await side.openSpotifySidePlayer(
		'https://open.spotify.com/playlist/abc',
		{},
		scope,
		async (url, s) => external.push([url, s])
	);
	assert.deepEqual(external, [['https://open.spotify.com/playlist/abc', scope]]);
	assert.deepEqual(popups, []);
	// The target contract still applies before the opener is reached.
	await assert.rejects(
		side.openSpotifySidePlayer(
			'https://open.spotify.com/account/overview',
			{},
			scope,
			async (url) => external.push([url])
		),
		/only opens/
	);
	assert.equal(external.length, 1);
	// Control: a browser tab never reaches the system-browser opener.
	const player = { opener: {}, location: { href: '' } };
	await side.openSpotifySidePlayer(
		'https://open.spotify.com/playlist/abc',
		{},
		{ open: () => player },
		async () => {
			throw new Error('a browser tab must not use the system opener');
		}
	);
	assert.equal(player.location.href, 'https://open.spotify.com/playlist/abc');
});

test('SPOT-6555b: a blocked player pop-up is reported, not swallowed', async () => {
	// window.open's own answer when the browser blocks a pop-up.
	const scope = { open: () => null };
	await assert.rejects(
		side.openSpotifySidePlayer('https://open.spotify.com/playlist/abc', {}, scope),
		/blocked the player window/
	);
});

test('SPOT-6555b: only open.spotify.com URLs reach the player', async () => {
	await assert.rejects(
		side.openSpotifySidePlayer('https://open.spotify.com.evil.example/x', {}, { open: () => {} }),
		/only opens/
	);
	await assert.rejects(
		side.openSpotifySidePlayer('javascript:alert(1)', {}, { open: () => {} }),
		/only opens/
	);
});

test('SPOT-6555b: the fallback opens only the content pages Electron opens', async () => {
	// The same contract as Electron's isSidePlayerOpenTarget.
	for (const bad of [
		'https://open.spotify.com/',
		'https://open.spotify.com/playlist/',
		'https://open.spotify.com/account/overview',
		'https://open.spotify.com/user/someone',
		'https://open.spotify.com/playlist/abc/extra',
		'https://open.spotify.com/playlist/a%2Fb',
		'http://open.spotify.com/playlist/abc'
	]) {
		let opened = false;
		await assert.rejects(
			side.openSpotifySidePlayer(
				bad,
				{},
				{ open: () => ((opened = true), { opener: {}, location: { href: '' } }) }
			),
			/only opens/,
			bad
		);
		assert.equal(opened, false, `${bad} opened no window`);
		assert.equal(side.isSpotifyPlayerTarget(bad), false, bad);
	}
	for (const kind of ['playlist', 'album', 'track', 'artist']) {
		const url = `https://open.spotify.com/${kind}/4uLU6hMCjMI75M1A2tKUQC?si=x`;
		assert.equal(side.isSpotifyPlayerTarget(url), true, kind);
		const player = { opener: {}, location: { href: '' } };
		await side.openSpotifySidePlayer(url, {}, { open: () => player });
		assert.equal(player.location.href, url);
	}
});

test('listCoreAudioOutputs forwards the Electron bridge and is absent elsewhere', async () => {
	const { scope, calls } = electronScope();
	assert.deepEqual(await mod.listCoreAudioOutputs(scope), [{ uid: 'hp' }]);
	assert.deepEqual(calls, [['listCoreAudioOutputs']]);
	assert.equal(await mod.listCoreAudioOutputs({}), null);
	assert.equal(await mod.listCoreAudioOutputs({ __TAURI_INTERNALS__: {} }), null);
});
